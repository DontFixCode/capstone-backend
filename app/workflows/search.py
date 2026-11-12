"""최초 검색 흐름: 정적 후보 생성 → 도보(TMAP) 조회 → 실시간 공유 수집 → 평가 → 추천 → 저장.

기존 핵심 함수(routing.build_search_index·generate_candidates, evaluation.evaluate_candidates,
recommendation.recommend_candidates)를 그대로 호출하고, 이 모듈은 입력 준비·결과 저장을 잇는 최소 어댑터만 둔다.

- 한 검색은 REPEATABLE READ 한 번으로 읽은 하나의 정적 공개본만 사용한다. DFS 중에는 외부 API를 호출하지 않는다.
- 도보는 정적 후보 생성 뒤, 같은 방향·좌표·옵션의 구간을 한 번만 TMAP으로 조회한다. 같은 정류소 환승은 조회하지 않는다.
  경로 없음·조회 실패·시간 미확인(미조회)을 구분하고 미확인 도보시간을 0으로 채우지 않는다.
- 실시간 대상(승하차 정류소 도착·노선 위치)은 검색 행(summary.realtime_targets, expires_at)에 등록해 워커가 공유 수집하고,
  검색 시점에는 같은 Phase 2 수집기(RealtimeCollector)로 간격 이내 공개값은 재사용·부족한 대상만 조회한다.
- 직접 ETA·관측 구간시간으로 하차 ETA를 만들지 못한 차량과, 기준 여유가 음수(선두 차량을 놓침)인 승차·환승 구간의 뒤
  차량(준비 시각 뒤에 오는 차량이 나올 때까지 한 대씩)만 앞차 ETA 기반 근사(Phase 3B)를 쓴다. 근사에 쓰는 노선 위치,
  목표 정류소(놓친 승차 정류소·하차 근사 대상 하차 정류소), 기준점 X 도착정보는 검색 시점에 새로 조회한다(재사용하지
  않음, 대상별 한 번, 같은 수집기). 노선 위치로 순서를 확인한 조합은 지난 예측을 지금 도착 중으로 본다. 다음 차량이
  아직 출발하지 않았으면(대전시 MSG_TP=07) 직전 차량 시각 + 노선 배차간격으로 예상 차량을 더하고, 순위는 실시간 후보 뒤에 준다
  (eta_source=headway_estimate, applicable_headway_seconds). X는 그
  시점 차량 위치로 정해지므로 워커 수집 대상에는 넣지 않는다.
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
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.db.session import Database
from app.integrations import route_shapes_api as route_shapes
from app.integrations.bus_api import TagoClient
from app.integrations.daejeon_bus_api import DaejeonBusClient
from app.integrations.walking_api import PROVIDER as WALK_PROVIDER, TmapWalkingClient, WalkQuery, WalkResponse
from app.modules.evaluation import repository as evaluation_repository
from app.modules.evaluation import samples as sample_rules
from app.modules.evaluation.eta import (
    EtaTable,
    build_ride_legs,
    following_board_etas,
    index_etas,
    index_segment_durations,
)
from app.modules.evaluation.schemas import (
    DESTINATION_REF,
    ORIGIN_REF,
    EtaReport,
    EvaluatedCandidate,
    EvaluationInputs,
    EvaluationPolicy,
    EvaluationResult,
    GapAnchor,
    ObservedEvent,
    RouteHeadway,
    SegmentObservation,
    VehicleEta,
    WalkResult,
)
from app.modules.evaluation.service import evaluate_candidates
from app.modules.recommendation.schemas import RecommendationResult
from app.modules.recommendation.service import recommend_candidates, uses_headway_estimate
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
    StaticRide,
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
    "search_round_pool_size": "BACKEND_SEARCH_ROUND_POOL_SIZE",
    "search_walk_cost_weight": "BACKEND_SEARCH_WALK_COST_WEIGHT",
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
            max_candidates=s.search_max_candidates, round_pool_size=s.search_round_pool_size,
            walk_cost_weight=s.search_walk_cost_weight, max_search_states=s.search_max_states,
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
    # 평가 허용 나이 이내 노선 위치 관측. 앞차 ETA 기반 근사의 차량 순서 판정에만 쓴다(위치에는 시각이 없다).
    positions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class LeadingGapNeeds:
    """앞차 ETA 기반 근사에 쓰는 입력 대상. 호출자가 이 검색에서 새로 받은 응답만 쓰도록 다시 조회한다."""

    anchor_stops: frozenset[str]  # 기준점 X
    target_stops: frozenset[str]  # 목표 정류소 D: 선두 차량을 놓치는 승차·환승 정류소와 하차 근사 대상 하차 정류소
    routes: frozenset[str]        # 차량 순서 판정에 노선 위치를 쓰는 노선


IN_PROGRESS_RETRIES = 3
IN_PROGRESS_WAIT_SECONDS = 1.0
# 한 번에 동시에 조회하는 도착정보 대상 수. 검색 한 번의 대상이 수십 개라 순서대로 부르면 대기 시간이 그대로 쌓인다.
ARRIVAL_COLLECT_WORKERS = 4


def collect_targets(db: Database, client: TagoClient | None, targets: list[RealtimeTarget],
                    interval_seconds: float, clock: Callable[[], datetime], *,
                    sleep: Callable[[float], None] | None = None,
                    daejeon_client: DaejeonBusClient | None = None, force: bool = False) -> dict[str, dict[str, Any]]:
    """Phase 2 수집기로 대상마다 간격 이내 공개값은 재사용하고 부족한 대상만 조회한다. force면 재사용하지 않는다.

    같은 대상을 다른 실행(워커의 주기 수집 등)이 수집 중이면(in_progress) 잠금을 기다리지 않고, 다른 대상을 먼저
    처리한 뒤 짧게 몇 번만 다시 확인한다. 끝까지 진행 중이면 그 시점의 마지막 공개값으로 평가한다(서로 막히지 않음).

    도착정보(대전시)는 대상마다 독립된 DB 연결·요청이라 ARRIVAL_COLLECT_WORKERS개씩 동시에 조회한다. 노선 위치(TAGO)는
    클라이언트가 호출 기록 콜백(on_attempt)을 인스턴스에 두므로 한 번에 하나씩, 도착정보와 나란히 조회한다.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    wait = sleep or time.sleep
    collector = transit_service.RealtimeCollector(db, client, interval_seconds=interval_seconds, clock=clock,
                                                  daejeon_client=daejeon_client)
    outcomes: dict[str, dict[str, Any]] = {}
    pending = sorted(targets, key=lambda t: (t.kind != "arrivals", t.external_id))
    for attempt in range(IN_PROGRESS_RETRIES + 1):
        if attempt:
            wait(IN_PROGRESS_WAIT_SECONDS)
        with ThreadPoolExecutor(max_workers=ARRIVAL_COLLECT_WORKERS, thread_name_prefix="arrivals") as pool:
            futures = {index: pool.submit(collector.collect, target, force=force)
                       for index, target in enumerate(pending) if target.kind == "arrivals"}
            collected = {index: collector.collect(target, force=force)
                         for index, target in enumerate(pending) if target.kind != "arrivals"}
            collected.update((index, future.result()) for index, future in futures.items())
        busy = []
        for index, target in enumerate(pending):
            outcome = collected[index]
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
                    eta_basis_policy: str = eta_basis.UNCONFIRMED, mark_not_departed: bool = False) -> RealtimeInputs:
    """대상별 마지막 공개 배치 전체를 읽어 평가 입력으로 바꾼다. 최신·오래됨·빈 결과·미제공·실패를 상태로 남긴다.

    ETA 기준 시각은 eta_basis 정책으로 정한다. 기본 정책은 원천이 주지 않은 기준 시각을 채우지 않는다(eta_basis_unconfirmed).
    mark_not_departed면 '다음 차량 출발 전' 응답(대전시 MSG_TP=07)을 ETA가 아니라 직전 차량 통과 기록으로 넘긴다
    (배차간격 기반 예상의 근거, eta.headway_legs). 최초 검색만 켜고 이동 안내 재계산은 기존 동작을 유지한다.
    """
    pattern_by_route: dict[str, list[str]] = {}
    for pattern in network.patterns:
        pattern_by_route.setdefault(pattern.route_external_id, []).append(pattern.pattern_key)
    reports: list[EtaReport] = []
    queried: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    refs: list[tuple[str, str | None]] = []
    rejected: Counter[str] = Counter()
    positions: list[dict[str, Any]] = []
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
        if target.kind == "positions" and states[snap.target_key]["within_evaluation_age"]:
            positions.extend(snap.observations)
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
            details = obs.get("details") or {}
            waiting = mark_not_departed and (
                (details.get("vehicle_match") or {}).get("reason") == "msg_tp_07_last_passed_vehicle")
            reports.append(EtaReport(
                pattern_id=pattern, stop_id=stop_ext, stop_order=obs["stop_seq"], vehicle_id=obs["vehicle_no"],
                run_key=obs["run_key"], remaining_seconds=obs["remaining_seconds"], basis_at=basis_at,
                basis_source=basis_source, stops_away=details.get("stops_away"),
                source_generated_at=obs["source_generated_at"], collected_at=obs["collected_at"],
                source_ref=f"transit_observation:{obs['id']}|batch:{snap.batch_key}",
                not_departed=waiting,
                last_passed_vehicle_id=(
                    ((details.get("source") or {}).get("CAR_REG_NO") or "").strip() or None if waiting else None),
            ))
    return RealtimeInputs(reports, frozenset(queried), states, refs, dict(rejected), positions)


def leading_gap_anchors(candidates: Iterable[StaticCandidate], realtime: RealtimeInputs,
                        segments: Iterable[SegmentObservation], *, cohort: str, context: SearchContext,
                        policy: EvaluationPolicy, board_rides: Mapping[tuple[str, int], datetime] | None = None,
                        previous_stops: Mapping[tuple[str, int], tuple[int, str]] | None = None,
                        probe_stops: set[str] | None = None,
                        rear_vehicles: set[tuple[str, str]] | None = None,
                        pattern_stops: Mapping[tuple[str, int], str] | None = None,
                        ) -> list[GapAnchor]:
    """앞차 ETA 기반 근사(Phase 3B)의 차량 조합을 고른다(설계 PREPARE_GAP_INPUTS).

    직접 ETA·관측 구간시간으로 하차 ETA를 만들지 못한 (탈 차량 B, 하차 정류소 D)마다 D의 선두 차량 A와, A가 마지막으로
    통과한 경유 위치 X를 고른다. X의 다음 차량이 B가 아니면 그 차량을 다시 앞차로 삼아 B가 나올 때까지 잇는다.
    X는 두 가지로 찾는다.
    - 도착정보(_arrival_links): 정류소 응답의 남은 정류장 수로 앞차가 마지막으로 지난 정류소를 정하고, 그 정류소 응답의
      다음 차량을 뒤 차량으로 본다. pattern_stops((패턴, 순번) → 정류소)가 있어야 하고, 아직 조회하지 않은 정류소는
      probe_stops에 더해 호출자가 조회한 뒤 이어 간다. 노선 위치에 없는 차량이 끼어도 이어진다.
    - 노선 위치(_alight_chain·_following_chain): 위치가 하나로 확인된 차량들의 순서로 여러 단계를 한 번에 고른다.
    같은 뒤 차량에 두 조합이 있으면 응답으로 확인한 도착정보 쪽을 쓴다. 시각은 도착정보로만 구한다
    (eta.leading_vehicle_gap_eta).

    board_rides는 승차 정류소의 선두 차량을 놓치는 (패턴, 승차 위치)와 그 구간의 준비 시각이다. 노선 위치상 선두 차량
    뒤 차량을 가까운 순으로 이어(_following_chain) 승차 정류소를 D로 한 조합을 고른다. 뒤 차량의 하차는 앞 차량 하차
    ETA에 같은 간격을 더하므로(eta.build_ride_legs) 하차 조합을 따로 고르지 않는다. previous_stops((패턴, 순번) → 바로 앞
    정류소, previous_stop_index)를 주면 선두 차량이 이미 승차 정류소에 있을 때 그 앞 정류소를 X로 쓴다.

    배차간격 기반 예상(eta.headway_legs)의 근거도 함께 고른다. 승차 정류소가 '다음 차량 출발 전'이면 그 정류소를 마지막으로
    지난 차량의 하차 조합을 고르고, 놓치는 구간에서 뒤 차량이 더 없으면 맨 뒤 차량이 마지막으로 지난 정류소를
    probe_stops에, 그 차량을 rear_vehicles에 더한다(그 정류소 응답이 '다음 차량 출발 전'인지 확인하도록 호출자가 조회).
    """
    table = index_etas(realtime.eta_reports, evaluated_at=context.evaluated_at,
                       max_data_age_seconds=policy.max_data_age_seconds)
    durations = index_segment_durations(segments, condition_key=cohort)
    located: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for obs in realtime.positions:
        if obs["pattern_key"] and obs["stop_seq"] is not None and obs["vehicle_no"]:
            located.setdefault((obs["pattern_key"], obs["vehicle_no"]), []).append(obs)
    rides = {(r.pattern_id, r.board_position, r.alight_position): r for c in candidates for r in c.rides}
    horizon_end_at = context.start_at + timedelta(seconds=policy.search_horizon_seconds)
    anchors: dict[tuple[str, int, str], GapAnchor] = {}
    for key in sorted(rides):
        ride = rides[key]
        legs = build_ride_legs(ride, table, durations, start_at=context.start_at,
                               queried_stop_ids=realtime.queried_stop_ids, policy=policy)
        at_target = table.by_position.get((ride.pattern_id, ride.alight_position, ride.alight_stop_id), ())
        leading = at_target[0].vehicle_id if at_target else None
        waiting = table.not_departed.get((ride.pattern_id, ride.board_position, ride.board_stop_id))
        trailing_vehicles = {leg.vehicle_id for leg in legs.legs if leg.alight_eta is None}
        if waiting is not None:
            trailing_vehicles.add(waiting.vehicle_id)
        for trailing in sorted(trailing_vehicles - {None, leading}):
            anchors.update(_alight_chain(ride, leading, trailing, located, previous_stops))
            if pattern_stops:
                anchors.update(_alight_chain_by_arrivals(ride, trailing, table, pattern_stops, probe_stops))
        ready_at = (board_rides or {}).get((ride.pattern_id, ride.board_position))
        if ready_at is not None:
            until = min(ready_at, horizon_end_at)
            anchors.update(_following_chain(ride, table, located, until=until,
                                            evaluated_at=context.evaluated_at, previous_stops=previous_stops,
                                            probe_stops=probe_stops, rear_vehicles=rear_vehicles))
            if pattern_stops:
                anchors.update(_following_chain_by_arrivals(ride, table, pattern_stops, until=until,
                                                            evaluated_at=context.evaluated_at,
                                                            probe_stops=probe_stops))
    return [anchors[key] for key in sorted(anchors)]


# 노선 위치로 한 번에 고르는 하차 조합의 최대 단계 수(단계마다 기준점 조회 1회).
ALIGHT_GAP_MAX_LINKS = 6
# 도착정보로 한 대씩 이어 가는 조합의 최대 단계 수(끝없이 이어지지 않도록 하는 안전 한도, 단계마다 기준점 조회 1회).
ARRIVAL_CHAIN_MAX_LINKS = 8


def pattern_stop_index(network: StaticNetwork) -> dict[tuple[str, int], str]:
    """(패턴, 경유 순번) → 정류소 원천 ID."""
    return {(pattern.pattern_key, stop.seq): stop.stop_external_id for pattern in network.patterns
            for stop in pattern.stops}


def _arrival_links(pattern_id: str, target_stop_id: str, target_order: int, table: EtaTable,
                   pattern_stops: Mapping[tuple[str, int], str], probe_stops: set[str] | None,
                   ) -> Iterator[tuple[str, int, str, VehicleEta]]:
    """도착정보만으로 목표 정류소 D에 오는 차량을 앞에서부터 한 대씩 찾는다. (앞 차량, X 순번, X 정류소, 뒤 차량의 X 예측).

    정류소 응답에 나온 차량의 남은 정류장 수만큼 거슬러 올라간 정류소가 그 차량이 마지막으로 지난 정류소 X이고, X 응답에
    나온 다음 차량이 뒤 차량이다. 그 뒤 차량의 X 응답 남은 정류장 수로 다시 거슬러 올라간다. X 응답에 앞 차량이 그대로
    나오면(정류장 수가 한두 개 어긋나 아직 X를 지나지 않음) X에서 다시 센다. X를 아직 조회하지 않았으면 probe_stops에
    더하고 멈춘다. X가 '다음 차량 출발 전'이거나 남은 정류장 수가 없으면 멈춘다.
    """
    listed = table.by_position.get((pattern_id, target_order, target_stop_id), ())
    if not listed or listed[0].vehicle_id is None:
        return
    at_order, lead = target_order, listed[0]
    seen = {lead.vehicle_id}
    for _ in range(ARRIVAL_CHAIN_MAX_LINKS * 2):
        if lead.stops_away is None:
            return
        x_order = at_order - max(lead.stops_away, 1)
        x_stop = pattern_stops.get((pattern_id, x_order))
        if x_stop is None:
            return
        at_x = table.by_position.get((pattern_id, x_order, x_stop), ())
        if not at_x:
            if (pattern_id, x_order, x_stop) not in table.not_departed and probe_stops is not None:
                probe_stops.add(x_stop)
            return
        following = at_x[0]
        if following.vehicle_id is None:
            return
        if following.vehicle_id == lead.vehicle_id:
            at_order, lead = x_order, following
            continue
        if following.vehicle_id in seen:
            return
        seen.add(following.vehicle_id)
        yield lead.vehicle_id, x_order, x_stop, following
        at_order, lead = x_order, following


def _arrival_anchor(ride: StaticRide, target_stop_id: str, target_order: int, leading: str, x_order: int, x_stop: str,
                    following: VehicleEta) -> GapAnchor:
    return GapAnchor(
        pattern_id=ride.pattern_id, target_stop_id=target_stop_id, target_stop_order=target_order,
        trailing_vehicle_id=following.vehicle_id, leading_vehicle_id=leading, anchor_stop_id=x_stop,
        anchor_stop_order=x_order, source_ref=f"arrivals:{following.source_ref}", order_confirmed=True)


def _alight_chain_by_arrivals(ride: StaticRide, trailing: str, table: EtaTable,
                              pattern_stops: Mapping[tuple[str, int], str], probe_stops: set[str] | None,
                              ) -> dict[tuple[str, int, str], GapAnchor]:
    """하차 정류소 D의 다음 차량부터 탈 차량 B가 나올 때까지 도착정보로 이은 하차 조합.

    B의 하차 시각 = D 다음 차량의 남은 시간 + 그 뒤 차량이 앞차 직전 정류소까지 남은 시간 + … + B가 앞차 직전 정류소까지
    남은 시간(eta.gap_chain_etas). B에 닿기 전에 조회하지 않은 정류소가 나오면 거기까지의 조합만 돌려준다(조회 뒤 이어짐).
    """
    chain: dict[tuple[str, int, str], GapAnchor] = {}
    links = _arrival_links(ride.pattern_id, ride.alight_stop_id, ride.alight_position, table, pattern_stops, probe_stops)
    for count, (leading, x_order, x_stop, following) in enumerate(links, start=1):
        chain[(ride.pattern_id, ride.alight_position, following.vehicle_id)] = _arrival_anchor(
            ride, ride.alight_stop_id, ride.alight_position, leading, x_order, x_stop, following)
        if following.vehicle_id == trailing or count >= ARRIVAL_CHAIN_MAX_LINKS:
            break
    return chain


def _following_chain_by_arrivals(ride: StaticRide, table: EtaTable, pattern_stops: Mapping[tuple[str, int], str], *,
                                 until: datetime, evaluated_at: datetime, probe_stops: set[str] | None,
                                 ) -> dict[tuple[str, int, str], GapAnchor]:
    """승차 정류소 D의 다음 차량을 놓칠 때, 그 뒤 차량을 도착정보로 한 대씩 이은 승차 조합.

    근사한 승차 ETA가 until(준비 시각) 이후인 차량이 나오면 멈춘다.
    """
    listed = table.by_position.get((ride.pattern_id, ride.board_position, ride.board_stop_id), ())
    if not listed or listed[0].predicted_arrival_at >= until:
        return {}
    chain: dict[tuple[str, int, str], GapAnchor] = {}
    links = _arrival_links(ride.pattern_id, ride.board_stop_id, ride.board_position, table, pattern_stops, probe_stops)
    for count, (leading, x_order, x_stop, following) in enumerate(links, start=1):
        chain[(ride.pattern_id, ride.board_position, following.vehicle_id)] = _arrival_anchor(
            ride, ride.board_stop_id, ride.board_position, leading, x_order, x_stop, following)
        etas = {eta.vehicle_id: eta for eta in following_board_etas(ride, table, chain, evaluated_at)[0]}
        estimate = etas.get(following.vehicle_id)
        if estimate is None or estimate.predicted_arrival_at >= until or count >= ARRIVAL_CHAIN_MAX_LINKS:
            break
    return chain
# 승차 근사에서 한 번에 미리 고르는 뒤 차량 수. 준비 시각 뒤 차량이 먼저 나오면 남는 조회(최대 이 수 − 1회)가 생기지만,
# 재평가 반복 횟수가 그만큼 준다.
FOLLOWING_LOOKAHEAD = 3


def _alight_chain(ride: StaticRide, leading: str | None, trailing: str,
                  located: dict[tuple[str, str], list[dict[str, Any]]],
                  previous_stops: Mapping[tuple[str, int], tuple[int, str]] | None,
                  ) -> dict[tuple[str, int, str], GapAnchor]:
    """하차 정류소 D의 선두 차량 A부터 탈 차량 B까지 노선 위치 순서로 이은 하차 조합(사이 차량 V1, V2 … 포함).

    각 단계의 X는 앞 차량이 마지막으로 통과한 위치다(앞 차량이 D에 있으면 D 바로 앞 정류소). 사이 차량이
    ALIGHT_GAP_MAX_LINKS 단계를 넘거나 순서가 모호하면 고르지 않는다.
    """
    if leading is None:
        return {}
    order = _vehicles_in_order(ride.pattern_id, leading, located)
    vehicles = [obs["vehicle_no"] for obs in order]
    if trailing not in vehicles or not 0 < vehicles.index(trailing) <= ALIGHT_GAP_MAX_LINKS \
            or order[0]["stop_seq"] > ride.alight_position:
        return {}
    at_target = order[0]["stop_seq"] == ride.alight_position
    chain: dict[tuple[str, int, str], GapAnchor] = {}
    for index, (lead, trail) in enumerate(zip(order, order[1:vehicles.index(trailing) + 1])):
        x = ((previous_stops or {}).get((ride.pattern_id, ride.alight_position)) if index == 0 and at_target
             else (lead["stop_seq"], lead["stop_external_id"]))
        if x is None or x[1] is None or trail["stop_seq"] >= x[0]:
            return {}
        chain[(ride.pattern_id, ride.alight_position, trail["vehicle_no"])] = GapAnchor(
            pattern_id=ride.pattern_id, target_stop_id=ride.alight_stop_id, target_stop_order=ride.alight_position,
            trailing_vehicle_id=trail["vehicle_no"], leading_vehicle_id=lead["vehicle_no"], anchor_stop_id=x[1],
            anchor_stop_order=x[0], source_ref=f"transit_observation:{lead['id']},{trail['id']}", order_confirmed=True)
    return chain


def previous_stop_index(network: StaticNetwork) -> dict[tuple[str, int], tuple[int, str]]:
    """(패턴, 경유 순번) → 같은 패턴에서 바로 앞 경유 위치(순번, 정류소 원천 ID)."""
    index: dict[tuple[str, int], tuple[int, str]] = {}
    for pattern in network.patterns:
        stops = sorted(pattern.stops, key=lambda stop: stop.seq)
        for before, stop in zip(stops, stops[1:]):
            index[(pattern.pattern_key, stop.seq)] = (before.seq, before.stop_external_id)
    return index


def _following_chain(ride: StaticRide, table: EtaTable, located: dict[tuple[str, str], list[dict[str, Any]]], *,
                     until: datetime, evaluated_at: datetime,
                     previous_stops: Mapping[tuple[str, int], tuple[int, str]] | None = None,
                     probe_stops: set[str] | None = None,
                     rear_vehicles: set[tuple[str, str]] | None = None,
                     ) -> dict[tuple[str, int, str], GapAnchor]:
    """승차 정류소 선두 차량 뒤 차량을 가까운 순으로 이은 조합. B1은 선두 차량, B2는 B1이 마지막으로 통과한 위치를 X로 한다.

    선두 차량이 노선 위치상 이미 승차 정류소에 있으면 B1의 X는 승차 정류소 바로 앞 정류소다(previous_stops).
    근사한 승차 ETA가 until(준비 시각) 이후인 차량이 나오면 멈춘다. 아직 근사할 수 없는 차량(X 미조회·확인 실패)은
    FOLLOWING_LOOKAHEAD대까지 미리 고르므로, 호출자가 그 X들을 한 묶음으로 조회한 뒤 다시 고르면 그만큼씩 늘어난다.
    노선 위치에 뒤 차량이 더 없는데도 준비 시각 전 차량뿐이면, 맨 뒤 차량이 마지막으로 지난 정류소를 probe_stops에,
    그 차량을 rear_vehicles((패턴, 차량))에 더한다(다음 차량이 아직 출발 전인지 그 정류소 응답으로 확인, eta.headway_legs).
    """
    listed = table.by_position.get((ride.pattern_id, ride.board_position, ride.board_stop_id), ())
    if not listed or listed[0].vehicle_id is None or listed[0].predicted_arrival_at >= until:
        return {}
    order = _vehicles_in_order(ride.pattern_id, listed[0].vehicle_id, located)
    if not order or order[0]["stop_seq"] > ride.board_position:
        return {}
    at_target = order[0]["stop_seq"] == ride.board_position
    first_x = ((previous_stops or {}).get((ride.pattern_id, ride.board_position)) if at_target
               else (order[0]["stop_seq"], order[0]["stop_external_id"]))
    if first_x is None:
        return {}
    listed_ids = {eta.vehicle_id for eta in listed}
    chain: dict[tuple[str, int, str], GapAnchor] = {}
    exhausted = True
    unknown = 0
    for index, (lead, trail) in enumerate(zip(order, order[1:])):
        x_seq, x_stop = first_x if index == 0 else (lead["stop_seq"], lead["stop_external_id"])
        if x_stop is None or trail["stop_seq"] >= x_seq:
            exhausted = False
            break
        vehicle = trail["vehicle_no"]
        if vehicle not in listed_ids:
            chain[(ride.pattern_id, ride.board_position, vehicle)] = GapAnchor(
                pattern_id=ride.pattern_id, target_stop_id=ride.board_stop_id, target_stop_order=ride.board_position,
                trailing_vehicle_id=vehicle, leading_vehicle_id=lead["vehicle_no"], anchor_stop_id=x_stop,
                anchor_stop_order=x_seq, source_ref=f"transit_observation:{lead['id']},{trail['id']}",
                order_confirmed=True)
        etas = {eta.vehicle_id: eta for eta in (*listed, *following_board_etas(ride, table, chain, evaluated_at)[0])}
        if vehicle not in etas:
            # X를 아직 조회하지 않았거나 확인에 실패한 차량. X는 노선 위치만으로 정해지므로 몇 대까지는 미리 골라
            # 호출자가 한 묶음으로 조회하게 한다(한 대씩 고르면 차량마다 재평가 한 바퀴가 든다).
            exhausted = False
            unknown += 1
            if unknown >= FOLLOWING_LOOKAHEAD:
                break
            continue
        if etas[vehicle].predicted_arrival_at >= until:
            exhausted = False
            break
    behind_rear = any(pattern == ride.pattern_id and obs["stop_seq"] < order[-1]["stop_seq"]
                      for (pattern, _), found in located.items() for obs in found)
    if exhausted and not behind_rear:
        # 노선 위치에 맨 뒤 차량보다 뒤인 차량이 없다. 그 차량이 마지막으로 지난 정류소 응답으로 다음 차량이 출발 전인지 확인한다.
        rear_seq, rear_stop = first_x if len(order) == 1 else (order[-1]["stop_seq"], order[-1]["stop_external_id"])
        if rear_stop is not None and rear_seq < ride.board_position:
            if probe_stops is not None:
                probe_stops.add(rear_stop)
            if rear_vehicles is not None:
                rear_vehicles.add((ride.pattern_id, order[-1]["vehicle_no"]))
    return chain


def _vehicles_in_order(pattern_id: str, leading_vehicle: str,
                       located: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """같은 패턴의 선두 차량과 그 뒤 차량 위치 관측(가까운 순). 위치가 하나로 확인되지 않거나 같은 위치에 여러 대가 있어
    순서가 모호해지는 지점 앞에서 멈춘다."""
    lead = located.get((pattern_id, leading_vehicle), [])
    on_pattern = [obs for (pattern, _), found in located.items() if pattern == pattern_id for obs in found]
    per_seq = Counter(obs["stop_seq"] for obs in on_pattern)
    if len(lead) != 1 or per_seq[lead[0]["stop_seq"]] != 1:
        return []
    order = [lead[0]]
    for obs in sorted((o for o in on_pattern if o["stop_seq"] < lead[0]["stop_seq"]), key=lambda o: -o["stop_seq"]):
        if per_seq[obs["stop_seq"]] != 1 or len(located[(pattern_id, obs["vehicle_no"])]) != 1:
            break
        order.append(obs)
    return order


_KST = timezone(timedelta(hours=9))
# 배차간격(분)으로 받아들이는 범위. 벗어나면 단위·값이 다른 것으로 보고 쓰지 않는다.
_HEADWAY_MINUTES = (1.0, 180.0)


def applicable_headway_seconds(service_info: dict[str, Any] | None, at: datetime) -> float | None:
    """평가 시각에 적용할 노선 배차간격(초). 아직 출발하지 않은 다음 차량의 예상(eta.headway_legs)에만 쓴다.

    TAGO 노선정보(getRouteInfoIem)의 배차간격을 분 단위로 보고 요일(평일·토·일, 한국 시각)로 고른다. 단위는 2026-11-12
    대전시 '출발 전' 응답의 직전 차량 통과 간격과 대조해 확인했다(60쌍, 절대오차 중앙값 3.2분, 하루치·검증 대기).
    첫차 전·막차 뒤(원값 HHMM)이거나 값이 없으면 None이다. 공휴일·시간대별 배차는 구분하지 못한다.
    """
    fields = (service_info or {}).get("fields") or {}

    def number(name: str) -> float | None:
        try:
            return float((fields.get(name) or {}).get("value"))
        except (TypeError, ValueError):
            return None

    def minute_of_day(name: str) -> float | None:
        value = number(name)
        return None if value is None else (value // 100) * 60 + value % 100

    local = at.astimezone(_KST)
    minutes = number({5: "intervalsattime", 6: "intervalsuntime"}.get(local.weekday(), "intervaltime"))
    if minutes is None or not _HEADWAY_MINUTES[0] <= minutes <= _HEADWAY_MINUTES[1]:
        return None
    first, last, now = minute_of_day("startvehicletime"), minute_of_day("endvehicletime"), local.hour * 60 + local.minute
    if first is not None and last is not None and first <= last and not first <= now <= last:
        return None
    return minutes * 60


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
        daejeon_client: DaejeonBusClient | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.tago_client = tago_client
        # 실시간 도착정보 원천(대전시). 위치정보는 tago_client로 수집한다.
        self.daejeon_client = daejeon_client
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

    def _leading_gap_targets(self, network: StaticNetwork, context: SearchContext, result: CandidateSearchResult,
                             walks: dict[tuple[str, str], WalkLookup], targets: list[RealtimeTarget],
                             board_rides: dict[tuple[str, int], datetime],
                             previous_stops: Mapping[tuple[str, int], tuple[int, str]],
                             headways: tuple[RouteHeadway, ...] = (),
                             ) -> tuple[LeadingGapNeeds, dict[tuple[str, int], datetime]]:
        """수집값으로 앞차 ETA 기반 근사가 필요한 차량 조합을 고르고, 근사에 쓰는 입력(기준점 X·승차 정류소 D·노선 위치)을 돌려준다.

        현재 수집값과 조합으로 평가해 기준 여유가 음수(선두 차량을 놓침)인 승차·환승 구간과 그 준비 시각(같은 구간은 가장
        늦은 시각)을 board_rides에 더하고, 그 구간의 뒤 차량 조합도 고른다. 여러 후보가 같은 X를 쓰면 한 번만 조회한다.
        노선 위치는 놓친 구간과 하차 ETA가 없거나 앞차 근사로 구한 구간의 노선만 돌려준다. 뒤 차량이 더 없는 놓친 구간은
        다음 차량이 출발 전인지 확인할 정류소(배차간격 기반 예상의 근거)를 기준점과 함께 돌려준다.
        """
        config, policy = self.config, self.config.evaluation_policy
        now = self.clock()
        probe = context.model_copy(update={"evaluated_at": max(now, context.evaluated_at)})
        cohort = sample_rules.condition_key("tago", policy, config.eta_basis_policy)
        with self.db.transaction(isolation="REPEATABLE READ") as conn:
            realtime = realtime_inputs(conn, targets, network, now=now,
                                       interval_seconds=config.realtime_interval_seconds,
                                       max_data_age_seconds=policy.max_data_age_seconds,
                                       eta_basis_policy=config.eta_basis_policy, mark_not_departed=True)
            segments = segment_observations(conn, [r.pattern_id for c in result.candidates for r in c.rides],
                                            cohort, until=probe.evaluated_at)
        probe_stops: set[str] = set()
        rear_vehicles: set[tuple[str, str]] = set()
        anchors = leading_gap_anchors(result.candidates, realtime, segments, cohort=cohort, context=probe,
                                      policy=policy, board_rides=board_rides, previous_stops=previous_stops,
                                      pattern_stops=pattern_stop_index(network),
                                      probe_stops=probe_stops, rear_vehicles=rear_vehicles)
        inputs = EvaluationInputs(
            data_snapshot_id="leading-gap-probe", sample_condition_key=cohort,
            walks=tuple(w for w in (lookup.as_walk_result() for lookup in walks.values()) if w is not None),
            eta_reports=tuple(realtime.eta_reports), queried_stop_ids=realtime.queried_stop_ids,
            segment_observations=tuple(segments), gap_anchors=tuple(anchors), route_headways=headways,
            rear_vehicles=frozenset(rear_vehicles))
        evaluation = evaluate_candidates(result, probe, inputs, policy)
        statics = {c.static_candidate_id: c for c in result.candidates}
        boarded = {(r.pattern_id, r.board_position): r for c in result.candidates for r in c.rides}
        missed = dict(board_rides)
        routes: set[str] = set()
        alight_stops: set[str] = set()
        for item in (*evaluation.candidates, *evaluation.limited):
            rides = statics[item.static_candidate_id].rides
            for link in item.connections:
                if link.status == "negative_slack" and link.ready_at is not None \
                        and link.connection_index < len(rides):
                    ride = rides[link.connection_index]
                    key = (ride.pattern_id, ride.board_position)
                    missed[key] = max(missed.get(key, link.ready_at), link.ready_at)
            for leg in item.legs:
                # 하차 ETA가 없거나 앞차 근사로 구한 실제 차량, 그리고 하차 시각을 못 구한 배차간격 예상 차량(재사용한
                # 하차 정류소 응답에 이미 지나간 차량이 남아 직전 차량을 찾지 못한 경우)은 하차 정류소·노선 위치를 새로 받는다.
                gap_or_missing = leg.alight_eta is None or leg.alight_eta.eta_source == "leading_vehicle_gap"
                estimated_without_alight = leg.board_eta.eta_source == "headway_estimate" and leg.alight_eta is None
                if (leg.vehicle_id is not None and gap_or_missing) or estimated_without_alight:
                    routes.add(leg.route_id)
                    alight_stops.add(leg.alight_stop_id)
        if missed != board_rides:
            probe_stops.clear()
            anchors = leading_gap_anchors(result.candidates, realtime, segments, cohort=cohort, context=probe,
                                          policy=policy, board_rides=missed, previous_stops=previous_stops,
                                          pattern_stops=pattern_stop_index(network),
                                          probe_stops=probe_stops)
        return LeadingGapNeeds(
            anchor_stops=frozenset({a.anchor_stop_id for a in anchors} | probe_stops),
            target_stops=frozenset(alight_stops | {boarded[key].board_stop_id for key in missed}),
            routes=frozenset(routes | {boarded[key].route_id for key in missed}),
        ), missed

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

        board_rides: dict[tuple[str, int], datetime] = {}
        previous_stops = previous_stop_index(network)
        # 아직 출발하지 않은 다음 차량의 예상에 쓰는 노선별 배차간격(출발 기준 시각의 요일·운행시간으로 고름).
        with self.db.transaction(isolation="REPEATABLE READ") as conn:
            service_infos = transit_repository.route_service_infos(
                conn, {r.route_id for c in result.candidates for r in c.rides})
        headways = tuple(
            RouteHeadway(route_id=route_id, headway_seconds=seconds)
            for route_id, info in sorted(service_infos.items())
            if (seconds := applicable_headway_seconds(info, context.start_at)) is not None)
        if self.collect and (self.tago_client is not None or self.daejeon_client is not None) and targets:
            summary["realtime_collection"] = collect_targets(self.db, self.tago_client, targets,
                                                             config.realtime_interval_seconds, self.clock,
                                                             daejeon_client=self.daejeon_client)
            if self.daejeon_client is not None and self.realtime_provider is None:
                # 근사에 쓰는 입력(노선 위치·승차 정류소·기준점)은 이 검색에서 새로 받은 응답만 쓴다(간격 이내 재사용분도
                # 대상별 한 번 다시 조회). 노선 위치·목표 정류소(놓친 승차·하차 근사 대상 하차)·기준점 X를 한 묶음으로
                # 받는다. 재사용한 노선 위치로 고른 X는 새 위치를 받은 뒤 달라질 수 있어 그때 한 번 더 조회될 수 있지만,
                # 묶음마다 입력을 다시 읽고 평가하는 반복 횟수가 준다.
                # 앞 구간 하차 ETA나 앞 차량의 근사 승차 ETA는 조회 뒤에야 나오므로 새로 받을 대상이 없을 때까지 반복한다.
                fresh: dict[str, set[str]] = {"arrivals": set(), "positions": set()}
                for target in targets:
                    outcome = summary["realtime_collection"].get(transit_service.realtime_target_key(target), {})
                    if outcome.get("action") == "published":
                        fresh.setdefault(target.kind, set()).add(target.external_id)
                source = frozenset({"route_search"})
                while True:
                    needs, board_rides = self._leading_gap_targets(network, context, result, walks, targets,
                                                                   board_rides, previous_stops, headways)
                    routes = needs.routes - fresh["positions"]
                    stops = (needs.target_stops | needs.anchor_stops) - fresh["arrivals"]
                    extra = [*(RealtimeTarget("positions", route, source) for route in sorted(routes)),
                             *(RealtimeTarget("arrivals", stop, source) for stop in sorted(stops))]
                    if not extra:
                        break
                    summary.setdefault("leading_gap_collection", {}).update(collect_targets(
                        self.db, self.tago_client, extra, config.realtime_interval_seconds, self.clock,
                        daejeon_client=self.daejeon_client, force=True))
                    fresh["positions"] |= routes
                    fresh["arrivals"] |= stops
                    targets = transit_service.merge_targets([targets, extra])
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
                                           eta_basis_policy=config.eta_basis_policy, mark_not_departed=True)
            segments = segment_observations(conn, [r.pattern_id for c in result.candidates for r in c.rides],
                                            cohort, until=context.evaluated_at)
            shapes = transit_repository.route_shape_infos(
                conn, {r.route_id for c in result.candidates for r in c.rides})
            error_samples = sample_rules.transfer_error_samples(
                evaluation_repository.labeled_samples(conn, cohort, before=context.evaluated_at))
            scenes = sample_rules.joint_scenes(
                evaluation_repository.complete_scenes(conn, cohort, before=context.evaluated_at))
        rear_vehicles: set[tuple[str, str]] = set()
        anchors = leading_gap_anchors(result.candidates, realtime, segments, cohort=cohort, context=context,
                                      policy=policy, board_rides=board_rides, previous_stops=previous_stops,
                                      pattern_stops=pattern_stop_index(network),
                                      rear_vehicles=rear_vehicles)
        summary["data_inputs"] = _jsonable(realtime.target_states)
        summary["eta_reports"] = {"usable_input_rows": len(realtime.eta_reports), "rejected": realtime.rejected}
        summary["leading_gap_anchors"] = {"count": len(anchors), "missed_board_rides": len(board_rides),
                                          "anchor_stops": sorted({a.anchor_stop_id for a in anchors})}
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
            joint_scenes=tuple(scenes), gap_anchors=tuple(anchors), route_headways=headways,
            rear_vehicles=frozenset(rear_vehicles),
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
                           "ranked": len(recommendation.ranked), "limited": len(recommendation.excluded),
                           # 배차간격 기반 예상 시각이 들어간 조합(순위는 경로마다 가장 빠른 하나만, 실시간 후보 뒤).
                           "estimated": sum(1 for item in timed if uses_headway_estimate(item))},
                "headway_estimate": {"route_headway_seconds": {h.route_id: h.headway_seconds for h in headways}},
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
