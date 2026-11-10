"""교통정보 정규화·검증·공개·최신성의 업무 규칙.

- 외부 통신은 app/integrations/bus_api.py의 TagoClient만 사용한다. 실제 HTTP 시도는 on_attempt 콜백으로 받아 즉시
  api_fetches에 커밋하므로, 이후 공개 트랜잭션을 되돌려도 호출 이력은 남는다.
- 정적 정보는 전체 페이지·참조·순서를 검증한 뒤에만 stops·routes·route_stops를 같은 data_version으로 한 트랜잭션에서
  공개한다. 한 노선이라도 수집·검증에 실패하면 공개하지 않고 마지막 정상본을 유지한다.
- 실시간 정보는 대상별 배치의 모든 페이지를 정규화한 뒤 같은 트랜잭션에서 관측과 published_at을 함께 저장한다.
  부분 수집·실패는 공개하지 않으며, 정상 빈 응답은 빈 스냅샷으로 공개한다.
- 위치정보와 도착정보를 특정 차량으로 임의 연결하지 않는다. 수집 시각·ETA 0·응답 소멸을 실제 도착으로 쓰지 않는다.
- 도착정보는 대전시 버스 도착정보 API(getArrInfoByStopID)에서 수집한다(2026-11-09 사용자 결정, TAGO 도착정보 대체).
  정류소 조회 조건 BusStopID는 대전시 정류소 ID(BUS_NODE_ID, 7자리)로, TAGO 정류소 ID에서 'DJB' 접두사를 뗀 값이다.
  정류소 번호(stop_no, TAGO nodeno, 5자리)를 보내면 운행 중에도 headerCd=4 '결과가 없습니다'가 온다(2026-11-10 실호출 확인:
  8002695·8001488은 정상, 12380·30720은 결과 없음). 응답의 BUS_NODE_ID·ROUTE_CD는
  'DJB' 접두사를 붙여 TAGO 정류소·노선 ID에 연결한다(실응답 대응 확인, 일반 규칙은 검증 전). 차량번호(CAR_REG_NO)를
  그대로 저장하고, 잔여초 기준 시각은 항목별 INFO_OFFER_TM(KST)으로 채택한다(사용자 결정, 검증 전·출처 표시).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg

from app.db.session import Database, try_xact_lock
from app.integrations import bus_api, daejeon_bus_api
from app.integrations.bus_api import AttemptRecord, Collection, Endpoint, TagoClient, field_state, text_value, to_int
from app.integrations.daejeon_bus_api import ArrivalsPage, DaejeonBusClient
from app.modules.transit import repository
from app.modules.transit.models import FetchAttemptRow, ObservationRow, RouteRow, RouteStopRow, StopRow
from app.modules.transit.schemas import (
    AttemptSummary,
    CollectOutcome,
    LatestSnapshot,
    NearbyStop,
    RealtimeTarget,
    StaticImportReport,
    StaticNetwork,
    StaticPattern,
    StaticPatternStop,
    StaticProblem,
    StaticRoute,
    StaticStop,
)

logger = logging.getLogger(__name__)

PROVIDER = repository.PROVIDER
CITY_CODE = bus_api.DAEJEON_CITY_CODE
# Phase 1 확인 CLI와 같은 페이지 크기. 호출량 산정(Phase 1 call_volume)도 이 값 기준이다.
PAGE_ROWS = 100
PATTERN_KEY_VERSION = "p1"
STATIC_NORMALIZER_VERSION = f"tago-static/1;pattern-key/{PATTERN_KEY_VERSION}"
POSITIONS_NORMALIZER_VERSION = "tago-positions/1"
ARRIVALS_NORMALIZER_VERSION = "tago-arrivals/1"
STATIC_LOCK = "transit:static"
# 정적 적재에서만, 제공기관 '가용한 세션이 존재하지 않습니다'(99) 응답을 90초 기다려 최대 3회 다시 요청한다(2026-11-07 결정).
STATIC_SESSION_RETRIES = 3
STATIC_SESSION_RETRY_WAIT_SECONDS = 90.0
# 인증 실패·호출 한도 초과 뒤의 요청은 같은 이유로 실패하므로 남은 대상을 호출하지 않는다(Phase 1 CLI와 같은 기준).
ABORT_CATEGORIES = frozenset({"auth", "rate_limit", "budget"})

SERVICE_BY_KIND = {
    "stops": "BusSttnInfoInqireService",
    "routes": "BusRouteInfoInqireService",
    "route_stops": "BusRouteInfoInqireService",
    "positions": "BusLcInfoInqireService",
    "arrivals": "ArvlInfoInqireService",
}
LIMIT_SETTING_BY_SERVICE = {
    "BusSttnInfoInqireService": "tago_limit_stops",
    "BusRouteInfoInqireService": "tago_limit_routes",
    "BusLcInfoInqireService": "tago_limit_positions",
    "ArvlInfoInqireService": "tago_limit_arrivals",
}
REALTIME_ENDPOINT = {"positions": bus_api.BUS_LOCATIONS, "arrivals": bus_api.ARRIVALS}
REALTIME_NORMALIZER = {"positions": POSITIONS_NORMALIZER_VERSION, "arrivals": ARRIVALS_NORMALIZER_VERSION}
# 실시간 도착정보 원천: 대전시 버스 도착정보(위치정보는 TAGO 그대로).
DAEJEON_PROVIDER = "daejeon"
DAEJEON_ARRIVALS_NORMALIZER_VERSION = "daejeon-arrivals/2"
# 다음 차가 기점·회차지에서 출발 전일 때의 항목(실조회로 추정, 검증 전). 차량번호를 쓰지 않는다.
DAEJEON_MSG_TP_WAITING_DEPARTURE = "07"
DAEJEON_ID_PREFIX = "DJB"
DAEJEON_ETA_BASIS_SOURCE = "daejeon_info_offer_tm"
KST = timezone(timedelta(hours=9))
# 응답 명세·실응답 기준으로 원천이 제공하지 않는 항목. 빈 값·실패와 구분해 표시한다.
NOT_PROVIDED = {
    "positions": ("source_generated_at", "run_key", "remaining_seconds"),
    "arrivals": ("run_key", "source_generated_at"),
}
_SECRET_PARAM_NAMES = frozenset({"servicekey", "appkey"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------- 키·좌표·패턴

def target_key(endpoint: Endpoint, params: dict[str, Any]) -> str:
    """제공기관·상세기능·조회 조건의 표준화 키. 페이지 조건과 인증키는 넣지 않는다."""
    clean = {
        str(k): str(v) for k, v in params.items()
        if v is not None and str(v) != "" and k not in ("pageNo", "numOfRows", "_type")
        and str(k).lower() not in _SECRET_PARAM_NAMES
    }
    query = "&".join(f"{k}={clean[k]}" for k in sorted(clean))
    return f"{PROVIDER}:{endpoint.operation}?{query}"


def realtime_target_key(target: RealtimeTarget, city_code: str = CITY_CODE) -> str:
    """대상의 수집 키(제공처:상세기능?조회 조건). 도착정보는 대전시 API, 위치정보는 TAGO다.

    대전시 도착정보는 기존 키와 같도록 내부 정류소 원천 ID(nodeId, 'DJB' 포함)로 키를 만든다. 실제 요청 값(BusStopID,
    접두사 제외)은 api_fetches.request_params에 남는다.
    """
    if target.kind == "arrivals":
        return f"{DAEJEON_PROVIDER}:{daejeon_bus_api.ARRIVALS_BY_STOP}?nodeId={target.external_id}"
    return target_key(REALTIME_ENDPOINT["positions"], {"cityCode": city_code, "routeId": target.external_id})


def daejeon_bus_stop_id(external_id: str) -> str | None:
    """대전시 BusStopID = TAGO 정류소 ID('DJB'+숫자)에서 접두사를 뗀 대전시 정류소 ID(BUS_NODE_ID). 형식이 다르면 None."""
    text = (external_id or "").strip()
    digits = text[len(DAEJEON_ID_PREFIX):] if text.startswith(DAEJEON_ID_PREFIX) else ""
    return digits if digits.isdigit() else None


def provider_of_key(key: str) -> str:
    return key.split(":", 1)[0]


def target_from_key(key: str, city_code: str = CITY_CODE, source: str | None = None) -> RealtimeTarget | None:
    """realtime_target_key의 역변환. 같은 키를 다시 만들 수 있는 실시간 대상만 돌려주고, 아니면 None."""
    daejeon_prefix = f"{DAEJEON_PROVIDER}:{daejeon_bus_api.ARRIVALS_BY_STOP}?nodeId="
    if key.startswith(daejeon_prefix):
        external_id = key[len(daejeon_prefix):]
        target = RealtimeTarget("arrivals", external_id, frozenset({source}) if source else frozenset())
        return target if external_id and realtime_target_key(target, city_code) == key else None
    for kind, endpoint in REALTIME_ENDPOINT.items():
        prefix = f"{PROVIDER}:{endpoint.operation}?"
        if not key.startswith(prefix):
            continue
        params = dict(part.split("=", 1) for part in key[len(prefix):].split("&") if "=" in part)
        external_id = params.get("routeId" if kind == "positions" else "nodeId")
        if not external_id:
            return None
        target = RealtimeTarget(kind, external_id, frozenset({source}) if source else frozenset())
        return target if realtime_target_key(target, city_code) == key else None
    return None


def parse_coordinate(lat_raw: Any, lon_raw: Any) -> tuple[float | None, float | None, str]:
    """(위도, 경도, 상태). 숫자가 아니거나 WGS84 범위 밖·(0, 0)·누락이면 좌표를 버리고 상태만 남긴다."""
    values = []
    for raw in (lat_raw, lon_raw):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None, None, "missing"
        if isinstance(raw, bool):
            return None, None, "non_numeric"
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None, None, "non_numeric"
        if not math.isfinite(number):
            return None, None, "non_numeric"
        values.append(number)
    lat, lon = values
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, "out_of_range"
    if lat == 0 and lon == 0:
        return None, None, "zero"
    return lat, lon, "valid"


def pattern_key(route_external_id: str, sequence: Sequence[tuple[str, str | None]]) -> str:
    """노선 ID·원천 방향값·정류소 순서열의 안정적인 서명. 순서가 같으면 정적 갱신 후에도 같은 값이다."""
    payload = json.dumps(
        {"provider": PROVIDER, "route": route_external_id, "stops": [[stop, direction] for stop, direction in sequence]},
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"{PATTERN_KEY_VERSION}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


# ---------------------------------------------------------------- HTTP 시도 기록

@dataclass
class RecordedAttempt:
    fetch_id: int
    page_no: int
    attempt_no: int
    result_status: str


@dataclass
class Batch:
    """한 대상의 전체 페이지 수집 1회. attempts는 실제 HTTP 시도 순서다."""

    batch_key: UUID
    api_kind: str
    target_key: str
    normalizer_version: str
    attempts: list[RecordedAttempt] = field(default_factory=list)
    collection: Collection | None = None

    @property
    def complete(self) -> bool:
        return self.collection is not None and self.collection.status in ("success", "empty")

    @property
    def ids(self) -> list[int]:
        return [a.fetch_id for a in self.attempts]

    def pages(self) -> list[tuple[int, int | None, list[dict[str, Any]]]]:
        """(page_no, 채택 fetch_id, items). 채택 행은 오류 없이 받은 그 페이지의 마지막 시도다."""
        result = []
        for page in (self.collection.pages if self.collection else []):
            page_no = int(page.params.get("pageNo", "1"))
            tried = [a for a in self.attempts if a.page_no == page_no]
            adopted = tried[-1].fetch_id if tried and page.error is None and tried[-1].result_status in (
                "success", "empty") else None
            result.append((page_no, adopted, page.items))
        return result

    def adopted_ids(self) -> list[int]:
        return [fetch_id for _, fetch_id, _ in self.pages() if fetch_id is not None]


def _result_status(record: AttemptRecord) -> str:
    if record.outcome == "ok":
        return "success" if record.item_count else "empty"
    return "timeout" if record.outcome == "timeout" else "error"


class FetchRecorder:
    """TagoClient.on_attempt 콜백. 시도마다 autocommit 연결로 api_fetches에 즉시 저장한다."""

    def __init__(self, conn: psycopg.Connection) -> None:
        if not conn.autocommit:
            raise ValueError("FetchRecorder에는 autocommit 연결이 필요합니다(공개 트랜잭션과 분리).")
        self.conn = conn
        self.batch: Batch | None = None
        self.counts: Counter[str] = Counter()

    def start(self, api_kind: str, key: str, normalizer_version: str) -> Batch:
        self.batch = Batch(uuid4(), api_kind, key, normalizer_version)
        return self.batch

    def __call__(self, record: AttemptRecord) -> None:
        batch = self.batch
        if batch is None:
            raise RuntimeError("배치를 시작하지 않은 상태의 HTTP 시도는 기록할 수 없습니다.")
        params = {k: v for k, v in record.params.items() if k.lower() not in _SECRET_PARAM_NAMES}
        params["_type"] = "json"
        status = _result_status(record)
        error_reason = None
        if record.error is not None:
            error_reason = f"{record.error.category}: {record.error.message}"[:2000]
        row = FetchAttemptRow(
            batch_key=batch.batch_key, provider=PROVIDER, api_kind=batch.api_kind, target_key=batch.target_key,
            request_params=params, page_no=int(params.get("pageNo", "1")), attempt_no=record.attempt_no,
            requested_at=parse_utc(record.started_at), received_at=parse_utc(record.received_at),
            http_status=record.http_status, provider_code=record.provider_code, result_status=status,
            total_count=record.total_count if record.total_count is None or record.total_count >= 0 else None,
            raw_body=record.body, normalizer_version=batch.normalizer_version, error_reason=error_reason,
        )
        fetch_id = repository.insert_fetch(self.conn, row)
        batch.attempts.append(RecordedAttempt(fetch_id, row.page_no, row.attempt_no, status))
        self.counts[SERVICE_BY_KIND[batch.api_kind]] += 1
        if record.attempt_no > 1:
            self.counts["retries"] += 1

    def finish(self, collection: Collection) -> Batch:
        batch = self.batch
        if batch is None:
            raise RuntimeError("시작한 배치가 없습니다.")
        batch.collection = collection
        self.batch = None
        return batch


def _batch_failure(batch: Batch) -> str:
    collection = batch.collection
    if collection is None:
        return "not_collected"
    error = collection.error
    detail = f"{error.category}: {error.message}" if error else ""
    return f"{collection.status}/{collection.stop_reason or 'none'}" + (f" ({detail})" if detail else "")


def _abort_category(batch: Batch) -> str | None:
    error = batch.collection.error if batch.collection else None
    if error is None:
        return None
    if error.category in ABORT_CATEGORIES or (batch.collection and batch.collection.stop_reason == "budget_exhausted"):
        return error.category
    return None


# ---------------------------------------------------------------- 정적 정규화·검증

@dataclass
class StaticDataset:
    stops: list[StopRow]
    routes: list[RouteRow]
    route_stops: list[RouteStopRow]
    adopted_fetch_ids: list[int]
    source: dict[str, Any]
    warnings: list[str]


def _index_items(batch: Batch) -> list[tuple[int | None, dict[str, Any]]]:
    return [(fetch_id, item) for _, fetch_id, items in batch.pages() for item in items]


def _service_info(item: dict[str, Any]) -> dict[str, Any]:
    """노선 기본정보의 운행 항목. 단위·형식을 확인하지 않았으므로 원천 값을 그대로 두고 상태를 함께 남긴다."""
    names = ("startvehicletime", "endvehicletime", "intervaltime", "intervalsattime", "intervalsuntime")
    return {
        "source_operation": bus_api.ROUTE_INFO.operation,
        "unit_status": "unconfirmed",
        "note": "첫차·막차·배차간격의 단위·형식은 확인 전이며 차량 시간표·구간 주행시간으로 쓰지 않는다.",
        "fields": {name: {"state": field_state(item, name), "value": item.get(name)} for name in names},
    }


def build_static_dataset(
    stop_batch: Batch, route_list_batch: Batch, route_batches: dict[str, tuple[Batch | None, Batch | None]],
) -> tuple[StaticDataset | None, list[StaticProblem]]:
    """전체 수집 결과를 검증해 공개할 행을 만든다. 문제가 하나라도 있으면 (None, 문제 목록)."""
    problems: list[StaticProblem] = []
    warnings: list[str] = []

    def problem(code: str, message: str, batch: Batch | None = None) -> None:
        problems.append(StaticProblem(code, message, batch.target_key if batch else None))

    for batch, label in ((stop_batch, "정류소 목록"), (route_list_batch, "노선 목록")):
        if not batch.complete:
            problem("collection_incomplete", f"{label} 수집 미완료: {_batch_failure(batch)}", batch)
        elif not batch.collection.items:
            problem("empty_list", f"{label}이 비어 있음", batch)
        elif batch.collection.total_count is not None and len(batch.collection.items) != batch.collection.total_count:
            problem("count_mismatch",
                    f"{label} 항목 {len(batch.collection.items)}개 ≠ totalCount {batch.collection.total_count}", batch)
    if problems:
        return None, problems

    # 정류소 목록: 원천 ID 문자열 기준. 같은 ID의 다른 내용은 병합하지 않고 보류한다.
    station: dict[str, tuple[int | None, dict[str, Any]]] = {}
    for fetch_id, item in _index_items(stop_batch):
        node_id = text_value(item, "nodeid")
        if node_id is None:
            problem("missing_id", "nodeid 없는 정류소 항목", stop_batch)
            continue
        if node_id in station:
            if station[node_id][1] != item:
                problem("conflicting_duplicate", f"정류소 {node_id}가 서로 다른 내용으로 중복", stop_batch)
            else:
                warnings.append(f"정류소 {node_id} 동일 항목 중복")
            continue
        station[node_id] = (fetch_id, item)

    route_list: dict[str, dict[str, Any]] = {}
    for _, item in _index_items(route_list_batch):
        route_id = text_value(item, "routeid")
        if route_id is None:
            problem("missing_id", "routeid 없는 노선 항목", route_list_batch)
            continue
        if route_id in route_list and route_list[route_id] != item:
            problem("conflicting_duplicate", f"노선 {route_id}가 서로 다른 내용으로 중복", route_list_batch)
        route_list.setdefault(route_id, item)

    routes: list[RouteRow] = []
    route_stops: list[RouteStopRow] = []
    route_only: dict[str, tuple[int | None, dict[str, Any], str]] = {}
    adopted: list[int] = stop_batch.adopted_ids() + route_list_batch.adopted_ids()
    direction_values: Counter[str] = Counter()
    repeated_visits = 0
    routes_with_repeats = 0
    routes_with_direction_change = 0
    for route_id in sorted(route_list):
        info_batch, stops_batch = route_batches.get(route_id, (None, None))
        if info_batch is None or stops_batch is None:
            problem("not_collected", f"노선 {route_id} 상세 미수집", info_batch or stops_batch)
            continue
        if not info_batch.complete or len(info_batch.collection.items) != 1:
            problem("route_info_invalid", f"노선 {route_id} 기본정보: {_batch_failure(info_batch)}, "
                    f"항목 {len(info_batch.collection.items) if info_batch.collection else 0}개", info_batch)
            continue
        if not stops_batch.complete or not stops_batch.collection.items:
            problem("route_stops_invalid", f"노선 {route_id} 경유 목록: {_batch_failure(stops_batch)}", stops_batch)
            continue
        info_fetch, info = _index_items(info_batch)[0]
        if text_value(info, "routeid") not in (None, route_id):
            problem("route_id_mismatch", f"노선 {route_id} 기본정보의 routeid 불일치", info_batch)
            continue
        total = stops_batch.collection.total_count
        if total is not None and len(stops_batch.collection.items) != total:
            problem("count_mismatch", f"노선 {route_id} 경유 {len(stops_batch.collection.items)}개 ≠ totalCount {total}",
                    stops_batch)
            continue
        entries: list[tuple[int, str, str | None, str, int | None, dict[str, Any]]] = []
        bad = False
        for fetch_id, item in _index_items(stops_batch):
            node_id = text_value(item, "nodeid")
            order = to_int(item.get("nodeord"))
            if node_id is None or order is None or order <= 0:
                problem("route_stop_invalid", f"노선 {route_id} 경유 항목의 nodeid·nodeord 누락·비정수", stops_batch)
                bad = True
                break
            if text_value(item, "routeid") not in (None, route_id):
                problem("route_id_mismatch", f"노선 {route_id} 경유 항목의 routeid 불일치", stops_batch)
                bad = True
                break
            entries.append((order, node_id, text_value(item, "updowncd"), str(item.get("nodeord")).strip(),
                            fetch_id, item))
        if bad:
            continue
        orders = [e[0] for e in entries]
        if len(set(orders)) != len(orders):
            problem("duplicate_node_order", f"노선 {route_id} 경유 순번(nodeord) 중복", stops_batch)
            continue
        entries.sort(key=lambda e: e[0])
        if orders and sorted(orders) != list(range(1, len(orders) + 1)):
            warnings.append(f"노선 {route_id} nodeord가 1부터 연속이 아님(seq는 순서대로 다시 매김, 원값 보존)")
        key = pattern_key(route_id, [(e[1], e[2]) for e in entries])
        visits = Counter(e[1] for e in entries)
        repeats = sum(count - 1 for count in visits.values() if count > 1)
        repeated_visits += repeats
        routes_with_repeats += 1 if repeats else 0
        directions = [e[2] for e in entries]
        direction_values.update(d if d is not None else "<missing>" for d in directions)
        if len({d for d in directions if d is not None}) > 1:
            routes_with_direction_change += 1
        for seq, (_, node_id, direction, order_text, fetch_id, item) in enumerate(entries, start=1):
            route_stops.append(RouteStopRow(route_id, key, direction, seq, order_text, node_id, fetch_id))
            if node_id not in station and node_id not in route_only:
                route_only[node_id] = (fetch_id, item, route_id)
        routes.append(RouteRow(
            external_id=route_id,
            route_no=text_value(info, "routeno"),
            route_type=text_value(info, "routetp"),
            start_name=text_value(info, "startnodenm"),
            end_name=text_value(info, "endnodenm"),
            service_info=_service_info(info),
            source_fetch_id=info_fetch,
        ))
        adopted += info_batch.adopted_ids() + stops_batch.adopted_ids()
    if problems:
        return None, problems

    coord_status: Counter[str] = Counter()
    stops: list[StopRow] = []
    for node_id, (fetch_id, item) in station.items():
        lat, lon, status = parse_coordinate(item.get("gpslati"), item.get("gpslong"))
        coord_status[status] += 1
        stops.append(StopRow(node_id, text_value(item, "nodenm"), text_value(item, "nodeno"), lat, lon, fetch_id,
                             "station_list"))
    for node_id, (fetch_id, item, _) in route_only.items():
        lat, lon, status = parse_coordinate(item.get("gpslati"), item.get("gpslong"))
        coord_status[f"route_only_{status}"] += 1
        stops.append(StopRow(node_id, text_value(item, "nodenm"), text_value(item, "nodeno"), lat, lon, fetch_id,
                             "route_stops"))
    source = {
        "station_list_total_count": stop_batch.collection.total_count,
        "station_list_items": len(stop_batch.collection.items),
        "station_list_unique_ids": len(station),
        "route_list_total_count": route_list_batch.collection.total_count,
        "route_list_unique_ids": len(route_list),
        "route_stop_entries": len(route_stops),
        "route_stop_unique_stops": len({rs.stop_external_id for rs in route_stops}),
        "route_only_stops": len(route_only),
        "station_list_stops_not_on_any_route": len(set(station) - {rs.stop_external_id for rs in route_stops}),
        "repeated_visit_entries": repeated_visits,
        "routes_with_repeated_visits": routes_with_repeats,
        "routes_with_direction_change": routes_with_direction_change,
        "direction_code_values": dict(direction_values),
        "coordinate_status": dict(coord_status),
        "patterns": len({(rs.route_external_id, rs.pattern_key) for rs in route_stops}),
    }
    return StaticDataset(stops, routes, route_stops, adopted, source, warnings), []


# ---------------------------------------------------------------- 정적 적재·공개

ClientFactory = Callable[..., TagoClient]


class StaticImportService:
    """정적 정보 초기 적재·일일 갱신. scripts/import_transit.py와 워커가 같은 절차를 쓴다."""

    def __init__(self, db: Database, client_factory: ClientFactory, *, rows: int = PAGE_ROWS,
                 city_code: str = CITY_CODE, session_retries: int = STATIC_SESSION_RETRIES,
                 session_retry_wait: float = STATIC_SESSION_RETRY_WAIT_SECONDS) -> None:
        self.db = db
        self.client_factory = client_factory
        self.rows = rows
        self.city_code = city_code
        self.session_retries = session_retries
        self.session_retry_wait = session_retry_wait

    def run(self) -> StaticImportReport:
        report = StaticImportReport(started_at=utc_now())
        lock_conn = self.db.connect()
        try:
            if not try_xact_lock(lock_conn, STATIC_LOCK):
                lock_conn.rollback()
                report.status = "skipped_locked"
                report.problems.append(StaticProblem("locked", "다른 정적 적재가 실행 중이라 호출하지 않음"))
                return report
            with self.db.connect(autocommit=True) as record_conn:
                recorder = FetchRecorder(record_conn)
                client = self.client_factory(on_attempt=recorder, session_retries=self.session_retries,
                                             session_retry_wait=self.session_retry_wait)
                try:
                    batches = self._collect(client, recorder)
                finally:
                    report.calls = {"by_service": dict(recorder.counts), "client_stats": client.stats,
                                    "http_attempts": client.request_count}
                    client.close()
                stop_batch, route_list_batch, route_batches, abort = batches
                if abort:
                    report.problems.append(StaticProblem("aborted", abort))
                    dataset = None
                else:
                    dataset, problems = build_static_dataset(stop_batch, route_list_batch, route_batches)
                    report.problems.extend(problems)
                if dataset is None:
                    lock_conn.rollback()
                    self._hold(record_conn, report, stop_batch, route_list_batch, route_batches)
                    return report
                report.source = dataset.source
                report.warnings = dataset.warnings
                try:
                    published_at = repository.clock_now(lock_conn)
                    version = uuid4()
                    report.loaded = repository.publish_static(
                        lock_conn, version, published_at, dataset.stops, dataset.routes, dataset.route_stops)
                    marked = repository.mark_published(lock_conn, dataset.adopted_fetch_ids, published_at)
                    report.verification = verify_static(lock_conn, dataset, version)
                    if marked != len(dataset.adopted_fetch_ids):
                        report.verification.setdefault("mismatches", []).append(
                            f"공개 표시 {marked}행 ≠ 채택 {len(dataset.adopted_fetch_ids)}행")
                    if report.verification.get("mismatches"):
                        raise StaticVerificationError("; ".join(report.verification["mismatches"][:5]))
                    lock_conn.commit()
                except Exception as exc:
                    lock_conn.rollback()
                    message = self.db.redact(f"{type(exc).__name__}: {exc}")[:500]
                    report.problems.append(StaticProblem("publish_failed", message))
                    repository.mark_unpublished(record_conn, dataset.adopted_fetch_ids,
                                                f"static_publish_failed: {message}", as_partial=False)
                    report.status = "held"
                    return report
                report.status = "published"
                report.data_version = version
                report.published_at = published_at
                return report
        finally:
            report.finished_at = utc_now()
            lock_conn.close()

    def _collect(self, client: TagoClient, recorder: FetchRecorder):
        city = {"cityCode": self.city_code}
        stop_batch = self._run_batch(recorder, "stops", target_key(bus_api.STATION_LIST, city),
                                     lambda: client.station_list(self.city_code, rows=self.rows))
        route_list_batch = Batch(uuid4(), "routes", target_key(bus_api.ROUTE_LIST, city), STATIC_NORMALIZER_VERSION)
        route_batches: dict[str, tuple[Batch | None, Batch | None]] = {}
        if not stop_batch.complete:
            return stop_batch, route_list_batch, route_batches, _abort_category(stop_batch) and (
                f"정류소 목록 수집 중단: {_batch_failure(stop_batch)}")
        route_list_batch = self._run_batch(recorder, "routes", route_list_batch.target_key,
                                           lambda: client.route_list(self.city_code, rows=self.rows))
        if not route_list_batch.complete:
            return stop_batch, route_list_batch, route_batches, _abort_category(route_list_batch) and (
                f"노선 목록 수집 중단: {_batch_failure(route_list_batch)}")
        route_ids: dict[str, None] = {}
        for item in route_list_batch.collection.items:
            route_id = text_value(item, "routeid")
            if route_id is not None:
                route_ids.setdefault(route_id, None)
        for route_id in sorted(route_ids):
            params = {"cityCode": self.city_code, "routeId": route_id}
            info = self._run_batch(recorder, "routes", target_key(bus_api.ROUTE_INFO, params),
                                   lambda: client.route_info(route_id, self.city_code))
            route_batches[route_id] = (info, None)
            if not info.complete:
                # 한 노선이라도 실패하면 이번 정적 교체는 보류되므로 남은 호출을 쓰지 않는다.
                return stop_batch, route_list_batch, route_batches, None
            stops = self._run_batch(recorder, "route_stops", target_key(bus_api.ROUTE_STOPS, params),
                                    lambda: client.route_stops(route_id, self.city_code, rows=self.rows))
            route_batches[route_id] = (info, stops)
            if not stops.complete:
                return stop_batch, route_list_batch, route_batches, None
        return stop_batch, route_list_batch, route_batches, None

    @staticmethod
    def _run_batch(recorder: FetchRecorder, api_kind: str, key: str, call: Callable[[], Collection]) -> Batch:
        recorder.start(api_kind, key, STATIC_NORMALIZER_VERSION)
        try:
            collection = call()
        except BaseException:
            recorder.batch = None
            raise
        return recorder.finish(collection)

    @staticmethod
    def _hold(conn: psycopg.Connection, report: StaticImportReport, stop_batch: Batch, route_list_batch: Batch,
              route_batches: dict[str, tuple[Batch | None, Batch | None]]) -> None:
        """공개하지 않고 사유를 기록한다. 마지막 정상본(현재 활성 행)은 건드리지 않는다."""
        report.status = "held"
        reason = "static_held: " + "; ".join(f"{p.code}: {p.message}" for p in report.problems)[:1500]
        batches = [stop_batch, route_list_batch] + [b for pair in route_batches.values() for b in pair if b]
        problem_targets = {p.target_key for p in report.problems if p.target_key}
        for batch in batches:
            if not batch.attempts:
                continue
            if not batch.complete:
                repository.mark_unpublished(conn, batch.ids, reason, as_partial=True)
            elif batch.target_key in problem_targets:
                repository.mark_unpublished(conn, batch.ids, reason, as_partial=False)


class StaticVerificationError(RuntimeError):
    """공개 트랜잭션 안의 대조가 실패했다. 트랜잭션을 되돌린다."""


def verify_static(conn: psycopg.Connection, dataset: StaticDataset, version: UUID) -> dict[str, Any]:
    """공개 직전(같은 트랜잭션) DB 활성 행을 원천에서 만든 행과 대조한다."""
    published = repository.published_static_rows(conn)
    mismatches: list[str] = []
    expected_stops = {
        s.external_id: (s.name, s.stop_no, None if s.lat is None else round(s.lat, 7),
                        None if s.lon is None else round(s.lon, 7), str(version))
        for s in dataset.stops
    }
    expected_routes = {r.external_id: (r.route_no, r.route_type, r.start_name, r.end_name, str(version))
                       for r in dataset.routes}
    expected_sequences: dict[str, list[tuple[Any, ...]]] = {}
    for rs in dataset.route_stops:
        expected_sequences.setdefault(rs.route_external_id, []).append(
            (rs.pattern_key, rs.direction_code, rs.seq, rs.source_node_order, rs.stop_external_id))
    for label, expected, actual in (("stops", expected_stops, published["stops"]),
                                    ("routes", expected_routes, published["routes"]),
                                    ("route_stops", expected_sequences, published["sequences"])):
        missing = set(expected) - set(actual)
        extra = set(actual) - set(expected)
        different = [k for k in set(expected) & set(actual) if expected[k] != actual[k]]
        if missing:
            mismatches.append(f"{label} 누락 {len(missing)}건 (예: {sorted(missing)[:3]})")
        if extra:
            mismatches.append(f"{label} 원천에 없는 활성 행 {len(extra)}건 (예: {sorted(extra)[:3]})")
        if different:
            mismatches.append(f"{label} 값 불일치 {len(different)}건 (예: {sorted(different)[:3]})")
    if published["route_stop_versions"] - {str(version)}:
        mismatches.append("route_stops에 다른 data_version이 섞임")
    versions = repository.current_static_versions(conn)
    if versions != [version]:
        mismatches.append(f"활성 routes의 data_version이 {len(versions)}개")
    return {
        "compared_stops": len(expected_stops),
        "compared_routes": len(expected_routes),
        "compared_route_stop_entries": len(dataset.route_stops),
        "mismatches": mismatches,
    }


def _raw_items(raw_body: str | None) -> list[dict[str, Any]] | None:
    try:
        body = json.loads(raw_body or "")["response"]["body"]
    except (ValueError, KeyError, TypeError):
        return None
    container = body.get("items") if isinstance(body, dict) else None
    if container in (None, "", {}):
        return []
    item = container.get("item") if isinstance(container, dict) else None
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else None


def verify_published_static(conn: psycopg.Connection) -> dict[str, Any]:
    """마지막 정적 공개에 채택된 api_fetches 원문을 다시 해석해 현재 활성 정적 행과 대조한다(적재 경로와 독립)."""
    last = repository.static_fetch_status(conn)["last_published_at"]
    if last is None:
        return {"state": "never_published", "mismatches": ["정적 공개본 없음"]}
    rows = conn.execute(
        "SELECT target_key, page_no, total_count, raw_body FROM app.api_fetches WHERE provider = %s "
        "AND api_kind IN ('stops', 'routes', 'route_stops') AND published_at = %s ORDER BY target_key, page_no",
        (PROVIDER, last),
    ).fetchall()
    mismatches: list[str] = []
    batches: dict[str, list[tuple[int, int | None, list[dict[str, Any]] | None]]] = {}
    for key, page_no, total, raw in rows:
        batches.setdefault(key, []).append((page_no, total, _raw_items(raw)))
    items_by_key: dict[str, list[dict[str, Any]]] = {}
    for key, pages in batches.items():
        numbers = [p[0] for p in pages]
        if numbers != list(range(1, len(numbers) + 1)):
            mismatches.append(f"{key}: 공개 페이지 번호가 1..N 연속이 아님 {numbers}")
        if any(p[2] is None for p in pages):
            mismatches.append(f"{key}: 원문을 해석할 수 없는 페이지")
        items = [item for p in pages for item in (p[2] or [])]
        total = pages[0][1]
        if total is not None and len(items) != total and "getRouteInfoIem" not in key:
            mismatches.append(f"{key}: 원문 항목 {len(items)}개 ≠ totalCount {total}")
        items_by_key[key] = items
    station_key = target_key(bus_api.STATION_LIST, {"cityCode": CITY_CODE})
    route_list_key = target_key(bus_api.ROUTE_LIST, {"cityCode": CITY_CODE})
    station_ids = {text_value(i, "nodeid") for i in items_by_key.get(station_key, [])}
    route_list_ids = {text_value(i, "routeid") for i in items_by_key.get(route_list_key, [])}
    source_sequences: dict[str, list[tuple[str | None, str | None, str]]] = {}
    for route_id in route_list_ids:
        key = target_key(bus_api.ROUTE_STOPS, {"cityCode": CITY_CODE, "routeId": route_id})
        if key not in items_by_key:
            mismatches.append(f"노선 {route_id}의 공개 경유 원문 없음")
            continue
        entries = sorted(items_by_key[key], key=lambda i: to_int(i.get("nodeord")) or 0)
        source_sequences[route_id] = [(text_value(i, "nodeid"), text_value(i, "updowncd"),
                                       str(i.get("nodeord")).strip()) for i in entries]
        info_key = target_key(bus_api.ROUTE_INFO, {"cityCode": CITY_CODE, "routeId": route_id})
        if len(items_by_key.get(info_key, [])) != 1:
            mismatches.append(f"노선 {route_id}의 공개 기본정보 원문이 1건이 아님")
    route_stop_ids = {stop for seq in source_sequences.values() for stop, _, _ in seq}
    published = repository.published_static_rows(conn)
    db_stops, db_routes = set(published["stops"]), set(published["routes"])
    expected_stops = station_ids | route_stop_ids
    for label, expected, actual in (("정류소", expected_stops, db_stops), ("노선", route_list_ids, db_routes)):
        if expected - actual:
            mismatches.append(f"{label} 원천에 있으나 활성 행 없음 {len(expected - actual)}건: {sorted(expected - actual)[:5]}")
        if actual - expected:
            mismatches.append(f"{label} 원천에 없는 활성 행 {len(actual - expected)}건: {sorted(actual - expected)[:5]}")
    sequence_mismatch = [
        route_id for route_id, seq in source_sequences.items()
        if [(stop, direction, order) for _, direction, _, order, stop in published["sequences"].get(route_id, [])]
        != [(stop, direction, order) for stop, direction, order in seq]
    ]
    if sequence_mismatch:
        mismatches.append(f"경유 순서 불일치 노선 {len(sequence_mismatch)}개: {sorted(sequence_mismatch)[:5]}")
    versions = repository.current_static_versions(conn)
    if len(versions) != 1 or published["route_stop_versions"] != {str(v) for v in versions}:
        mismatches.append("정적 3개 테이블의 활성 data_version이 하나가 아님")
    return {
        "state": "verified" if not mismatches else "mismatch",
        "published_at": last,
        "data_version": [str(v) for v in versions],
        "source": {
            "published_pages": len(rows),
            "station_list_total_count": (batches.get(station_key) or [(None, None, None)])[0][1],
            "station_list_unique_ids": len(station_ids),
            "route_list_unique_ids": len(route_list_ids),
            "route_stop_entries": sum(len(s) for s in source_sequences.values()),
            "route_stop_unique_stops": len(route_stop_ids),
            "route_only_stops": len(route_stop_ids - station_ids),
            "station_list_stops_not_on_any_route": len(station_ids - route_stop_ids),
        },
        "loaded": {
            "active_stops": len(db_stops),
            "active_routes": len(db_routes),
            "route_stop_entries": sum(len(v) for v in published["sequences"].values()),
        },
        "mismatches": mismatches,
    }


def static_status(conn: psycopg.Connection, now: datetime | None = None) -> dict[str, Any]:
    """현재 정적 공개본 버전·건수와 갱신 보류 여부(마지막 시도가 마지막 공개보다 늦으면 보류 또는 진행 중)."""
    versions = repository.current_static_versions(conn)
    status = repository.static_fetch_status(conn)
    last_attempt, last_published = status["last_attempt_at"], status["last_published_at"]
    if last_published is None:
        state = "never_published" if last_attempt else "never_attempted"
    elif last_attempt and last_attempt > last_published:
        state = "held_or_running"
    else:
        state = "published"
    return {
        "state": state,
        "data_versions": [str(v) for v in versions],
        "mixed_versions": len(versions) > 1,
        "last_attempt_at": last_attempt,
        "last_published_at": last_published,
        "reasons_since_last_publish": status["reasons_since_last_publish"],
        "counts": repository.static_counts(conn),
        "checked_at": now or utc_now(),
    }


def read_static_network(db: Database) -> StaticNetwork | None:
    """현재 정적 공개본을 REPEATABLE READ 한 번으로 읽는다. 공개본이 없거나 버전이 섞였으면 None."""
    with db.transaction(isolation="REPEATABLE READ") as conn:
        versions, stop_rows, pattern_rows, route_rows = repository.read_static_network(conn)
    if len(versions) != 1:
        return None
    stops = tuple(StaticStop(sid, ext, name, lat, lon) for sid, ext, name, lat, lon in stop_rows)
    grouped: dict[tuple[str, str], list[StaticPatternStop]] = {}
    for route_ext, key, seq, stop_ext, direction in pattern_rows:
        grouped.setdefault((route_ext, key), []).append(StaticPatternStop(seq, stop_ext, direction))
    patterns = tuple(StaticPattern(route_ext, key, tuple(stops_)) for (route_ext, key), stops_ in grouped.items())
    routes = tuple(StaticRoute(rid, ext, no, tp) for rid, ext, no, tp in route_rows)
    return StaticNetwork(versions[0], stops, patterns, routes)


def find_nearby_stops(db: Database, lat: float, lon: float, radius_m: float, *, limit: int = 50) -> list[NearbyStop]:
    """저장 좌표 기준 미터 반경 검색. 좌표 미확인(NULL) 정류소는 제외되며 결과는 도보 가능 확정이 아니다."""
    _, _, status = parse_coordinate(lat, lon)
    if status != "valid":
        raise ValueError(f"검색 좌표가 유효하지 않습니다: {status}")
    if not (isinstance(radius_m, (int, float)) and math.isfinite(radius_m) and radius_m > 0):
        raise ValueError("radius_m은 0보다 큰 유한한 미터 값이어야 합니다.")
    if limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다.")
    with db.transaction() as conn:
        rows = repository.nearby_stops(conn, lat, lon, float(radius_m), limit)
    return [NearbyStop(sid, ext, name, no, float(dist), float(slat), float(slon))
            for sid, ext, name, no, dist, slat, slon in rows]


def usage_report(db: Database, settings: Any, since: datetime, until: datetime) -> dict[str, Any]:
    """서비스별 실제 HTTP 시도 수와 설정한 승인 한도. 한도 미설정은 '미확인'으로 표시한다."""
    with db.transaction() as conn:
        rows = repository.usage(conn, since, until)
    services: dict[str, dict[str, Any]] = {}
    for row in rows:
        service = SERVICE_BY_KIND[row["api_kind"]]
        entry = services.setdefault(service, {"by_api_kind": {}, "http_attempts": 0})
        entry["by_api_kind"][row["api_kind"]] = {k: v for k, v in row.items() if k != "api_kind"}
        entry["http_attempts"] += row["http_attempts"]
    for service, setting in LIMIT_SETTING_BY_SERVICE.items():
        entry = services.setdefault(service, {"by_api_kind": {}, "http_attempts": 0})
        limit = getattr(settings, setting, None)
        entry["approved_limit"] = limit if limit is not None else "미확인"
        entry["limit_setting"] = f"BACKEND_{setting.upper()}"
        entry["within_limit"] = None if limit is None else entry["http_attempts"] <= limit
    return {
        "window": {"since": since, "until": until},
        "note": "기간 경계·차감 기준은 계정 승인 조건을 확인해야 한다. 공개 포털 표기를 승인 한도로 쓰지 않는다.",
        "services": services,
    }


# ---------------------------------------------------------------- 실시간 정규화

def _visit_match(visits: list[dict[str, Any]], stop_ext: str) -> tuple[str | None, int | None, dict[str, Any]]:
    """정류소가 현재 패턴에 한 번만 나오면 그 경유 위치를, 반복·미경유면 미확인과 사유를 반환한다."""
    hits = [v for v in visits if v["stop_external_id"] == stop_ext]
    if len(hits) == 1:
        return hits[0]["pattern_key"], hits[0]["seq"], {"status": "matched", "basis": "unique_visit_in_pattern"}
    if not hits:
        return None, None, {"status": "unmatched", "reason": "stop_not_on_current_pattern"}
    return None, None, {"status": "ambiguous", "reason": "repeated_visit",
                        "candidate_seqs": [v["seq"] for v in hits]}


def normalize_positions(
    conn: psycopg.Connection, request_route_id: str, pages: list[tuple[int, int, list[dict[str, Any]]]],
    collected: dict[int, datetime], static_version: str | None,
) -> list[ObservationRow]:
    """노선별 버스 위치. 응답에 routeid가 없어 노선은 요청 조건으로만 연결하고, 경유 위치는 nodeid·nodeord가 현재
    패턴의 같은 행과 일치할 때만 확정한다. 원천 관측 시각은 제공되지 않는다."""
    route_map = repository.route_ids(conn, [request_route_id])
    route_internal = route_map.get(request_route_id)
    visits = repository.route_visits(conn, [route_internal]).get(route_internal, []) if route_internal else []
    stop_map = repository.stop_ids(conn, [text_value(i, "nodeid") or "" for _, _, items in pages for i in items])
    rows: list[ObservationRow] = []
    for _, fetch_id, items in pages:
        for index, item in enumerate(items):
            node_id = text_value(item, "nodeid")
            order_text = text_value(item, "nodeord")
            lat, lon, coord = parse_coordinate(item.get("gpslati"), item.get("gpslong"))
            pattern, seq = None, None
            if route_internal is None:
                position = {"status": "unmatched", "reason": "route_not_in_current_static"}
            elif node_id is None or order_text is None:
                position = {"status": "unmatched", "reason": "nodeid_or_nodeord_missing"}
            else:
                same = [v for v in visits if v["source_node_order"] == order_text]
                if len(same) == 1 and same[0]["stop_external_id"] == node_id:
                    pattern, seq = same[0]["pattern_key"], same[0]["seq"]
                    position = {"status": "matched", "basis": "nodeid_and_nodeord_match_route_stops"}
                elif not same:
                    position = {"status": "unmatched", "reason": "nodeord_not_in_current_pattern"}
                else:
                    position = {"status": "unmatched", "reason": "nodeord_stop_mismatch"}
            rows.append(ObservationRow(
                fetch_id=fetch_id, item_index=index, kind="position", collected_at=collected[fetch_id],
                route_id=route_internal, stop_id=stop_map.get(node_id) if node_id else None,
                pattern_key=pattern, stop_seq=seq, vehicle_no=text_value(item, "vehicleno"),
                lat=lat, lon=lon,
                details={
                    "source": {k: item.get(k) for k in ("routenm", "routetp", "nodeid", "nodenm", "nodeord",
                                                        "vehicleno", "gpslati", "gpslong") if k in item},
                    "request": {"routeId": request_route_id},
                    "route_match": {"status": "matched" if route_internal else "unmatched",
                                    "basis": "request_route_id (응답에 routeid 없음)"},
                    "stop_match": {"status": "matched" if node_id in stop_map else "unmatched"},
                    "position_match": position,
                    "location_status": coord,
                    "vehicle_no_status": "provided" if text_value(item, "vehicleno") else field_state(item, "vehicleno"),
                    "not_provided": list(NOT_PROVIDED["positions"]),
                    "collected_at_meaning": "server_received_at (원천 관측 시각 아님)",
                    "static_version": static_version,
                },
            ))
    return rows


def normalize_arrivals(
    conn: psycopg.Connection, request_stop_id: str, pages: list[tuple[int, int, list[dict[str, Any]]]],
    collected: dict[int, datetime], static_version: str | None,
) -> list[ObservationRow]:
    """정류소별 도착예정. 기본 도착정보에는 차량번호·기준 시각이 없어 차량을 연결하지 않고 절대 ETA를 만들지 않는다.
    잔여초(arrtime)는 원값 그대로 저장한다."""
    items_all = [i for _, _, items in pages for i in items]
    route_map = repository.route_ids(conn, [text_value(i, "routeid") or "" for i in items_all])
    visits_by_route = repository.route_visits(conn, route_map.values())
    stop_map = repository.stop_ids(conn, [text_value(i, "nodeid") or request_stop_id for i in items_all]
                                   + [request_stop_id])
    rows: list[ObservationRow] = []
    for _, fetch_id, items in pages:
        for index, item in enumerate(items):
            route_ext = text_value(item, "routeid")
            node_id = text_value(item, "nodeid") or request_stop_id
            route_internal = route_map.get(route_ext) if route_ext else None
            pattern, seq, visit = None, None, {"status": "unmatched", "reason": "route_not_in_current_static"}
            if route_internal is not None:
                pattern, seq, visit = _visit_match(visits_by_route.get(route_internal, []), node_id)
            arrtime = to_int(item.get("arrtime"))
            remaining = arrtime if arrtime is not None and arrtime >= 0 else None
            rows.append(ObservationRow(
                fetch_id=fetch_id, item_index=index, kind="arrival", collected_at=collected[fetch_id],
                route_id=route_internal, stop_id=stop_map.get(node_id), pattern_key=pattern, stop_seq=seq,
                remaining_seconds=remaining,
                details={
                    "source": {k: item.get(k) for k in ("nodeid", "nodenm", "routeid", "routeno", "routetp",
                                                        "arrprevstationcnt", "vehicletp", "arrtime") if k in item},
                    "request": {"nodeId": request_stop_id},
                    "route_match": {"status": "matched" if route_internal else "unmatched", "basis": "response_routeid"},
                    "stop_match": {"status": "matched" if node_id in stop_map else "unmatched",
                                   "basis": "response_nodeid" if text_value(item, "nodeid") else "request_node_id"},
                    "visit_match": visit,
                    "vehicle_match": {"status": "not_provided",
                                      "reason": "기본 도착정보에 차량번호가 없어 위치정보 차량과 연결하지 않음"},
                    "remaining_seconds_status": "provided" if remaining is not None else field_state(item, "arrtime"),
                    "eta": {"basis": "unconfirmed",
                            "reason": "원천 기준 시각 미제공·근사 정책 미확정이라 절대 ETA를 계산하지 않음",
                            "zero_is_not_arrival": True},
                    "not_provided": list(NOT_PROVIDED["arrivals"]),
                    "collected_at_meaning": "server_received_at (원천 생성 시각 아님)",
                    "static_version": static_version,
                },
            ))
    return rows


def parse_kst(text: str | None) -> datetime | None:
    """대전시 응답의 'YYYY-MM-DD HH:MM:SS'(시간대 없음, KST)를 UTC로. 형식이 다르면 None."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_daejeon_arrivals(
    conn: psycopg.Connection, request_stop_id: str, bus_stop_id: str, fetch_id: int, items: list[dict[str, str]],
    collected_at: datetime, static_version: str | None,
) -> list[ObservationRow]:
    """대전시 정류소별 도착정보. 차량번호를 그대로 저장하고 운행 회차는 원천이 주지 않으므로 비워 둔다.

    잔여초(EXTIME_SEC)는 원값으로 저장하고, 기준 시각은 INFO_OFFER_TM(KST)을 채택해 절대 ETA를 함께 저장한다
    (기준 출처 daejeon_info_offer_tm, 검증 전). 기준 시각을 해석할 수 없으면 절대 ETA를 만들지 않는다.
    """
    def candidates(value: str | None) -> list[str]:
        """원천 ID 후보: 'DJB' 접두사를 붙인 값(TAGO 형식)을 먼저, 이미 접두사가 있거나 형식이 다른 값은 그대로."""
        text = (value or "").strip()
        return [f"{DAEJEON_ID_PREFIX}{text}", text] if text else []

    route_map = repository.route_ids(conn, [c for i in items for c in candidates(i.get("ROUTE_CD"))])
    visits_by_route = repository.route_visits(conn, route_map.values())
    stop_map = repository.stop_ids(conn, [c for i in items for c in candidates(i.get("BUS_NODE_ID"))]
                                   + [request_stop_id])

    def resolve(value: str | None, known: dict[str, int]) -> str | None:
        options = candidates(value)
        return next((c for c in options if c in known), options[0] if options else None)

    rows: list[ObservationRow] = []
    for index, item in enumerate(items):
        route_ext = resolve(item.get("ROUTE_CD"), route_map)
        node_ext = resolve(item.get("BUS_NODE_ID"), stop_map) or request_stop_id
        route_internal = route_map.get(route_ext) if route_ext else None
        pattern, seq, visit = None, None, {"status": "unmatched", "reason": "route_not_in_current_static"}
        if route_internal is not None:
            pattern, seq, visit = _visit_match(visits_by_route.get(route_internal, []), node_ext)
        remaining = to_int(item.get("EXTIME_SEC"))
        remaining = remaining if remaining is not None and remaining >= 0 else None
        basis = parse_kst(item.get("INFO_OFFER_TM"))
        predicted = basis + timedelta(seconds=remaining) if basis is not None and remaining is not None else None
        stops_away = to_int(item.get("STATUS_POS"))
        plate = (item.get("CAR_REG_NO") or "").strip() or None
        # MSG_TP=07: 다음 차가 기점·회차지에서 출발 전일 때의 항목으로 보이며, 차량번호는 직전에 지나간 차다
        # (2026-11-10 실조회, 검증 전). 시각은 그대로 두고 차량번호는 쓰지 않는다(원값은 details.source에 남음).
        waiting_departure = (item.get("MSG_TP") or "").strip() == DAEJEON_MSG_TP_WAITING_DEPARTURE
        vehicle = None if waiting_departure else plate
        if waiting_departure:
            vehicle_match = {"status": "not_used", "reason": "msg_tp_07_last_passed_vehicle",
                             "decision": "2026-11-10 사용자 결정(시각 유지·차량번호 제외, 검증 전)"}
        else:
            vehicle_match = {"status": "provided", "basis": "CAR_REG_NO"} if vehicle else {"status": "not_provided"}
        rows.append(ObservationRow(
            fetch_id=fetch_id, item_index=index, kind="arrival", collected_at=collected_at,
            route_id=route_internal, stop_id=stop_map.get(node_ext), pattern_key=pattern, stop_seq=seq,
            vehicle_no=vehicle, remaining_seconds=remaining,
            eta_basis_at=basis if predicted is not None else None, predicted_arrival_at=predicted,
            details={
                "source": dict(item),
                "request": {"BusStopID": bus_stop_id, "nodeId": request_stop_id},
                "route_match": {"status": "matched" if route_internal else "unmatched",
                                "basis": "ROUTE_CD + DJB prefix (2026-11-09 실응답 대응 확인, 일반 규칙 검증 전)"},
                "stop_match": {"status": "matched" if node_ext in stop_map else "unmatched",
                               "basis": "BUS_NODE_ID + DJB prefix" if item.get("BUS_NODE_ID") else "request_node_id",
                               "same_as_request": node_ext == request_stop_id},
                "visit_match": visit,
                "vehicle_match": vehicle_match,
                # STATUS_POS: 남은 정류장 수로 보이는 값(검증 전). 같은 차량의 승차·하차 예측을 잇는 일관성 확인에 쓴다.
                "stops_away": stops_away,
                "remaining_seconds_status": "provided" if remaining is not None else field_state(item, "EXTIME_SEC"),
                "eta": {"basis": DAEJEON_ETA_BASIS_SOURCE if predicted is not None else "unparsed",
                        "verification": "unverified", "decision": "2026-11-09 사용자 결정(INFO_OFFER_TM 기준)",
                        "zero_is_not_arrival": True},
                "eta_basis_source": DAEJEON_ETA_BASIS_SOURCE if predicted is not None else None,
                "not_provided": list(NOT_PROVIDED["arrivals"]),
                "collected_at_meaning": "server_received_at (원천 생성 시각 아님)",
                "static_version": static_version,
            },
        ))
    return rows


# ---------------------------------------------------------------- 실시간 수집·최신 조회

class RealtimeCollector:
    """대상별 실시간 수집. 같은 대상은 DB 잠금으로 한 번만 호출하고, 간격 이내 공개값이 있으면 재사용한다."""

    def __init__(self, db: Database, client: TagoClient | None, *, interval_seconds: float, city_code: str = CITY_CODE,
                 rows: int = PAGE_ROWS, clock: Callable[[], datetime] = utc_now,
                 daejeon_client: DaejeonBusClient | None = None) -> None:
        self.db = db
        self.client = client
        self.daejeon_client = daejeon_client
        self.interval = timedelta(seconds=interval_seconds)
        self.city_code = city_code
        self.rows = rows
        self.clock = clock

    def collect(self, target: RealtimeTarget, *, force: bool = False) -> CollectOutcome:
        key = realtime_target_key(target, self.city_code)
        if (self.daejeon_client if target.kind == "arrivals" else self.client) is None:
            source = "대전시 버스 API" if target.kind == "arrivals" else "TAGO"
            return CollectOutcome(key, "failed", reason=f"{source} 클라이언트가 설정되지 않아 조회하지 않음")
        lock_conn = self.db.connect()
        try:
            if not try_xact_lock(lock_conn, f"transit:realtime:{key}"):
                lock_conn.rollback()
                return CollectOutcome(key, "in_progress", reason="다른 실행이 같은 대상을 수집 중")
            if not force:
                pages = repository.latest_published_pages(lock_conn, target.kind, key)
                if pages:
                    collected = min(p["received_at"] for p in pages)
                    if self.clock() - collected < self.interval:
                        lock_conn.rollback()
                        return CollectOutcome(key, "reused", batch_key=pages[0]["batch_key"],
                                              reason="수집 간격 이내 공개값 재사용")
            if target.kind == "arrivals":
                return self._collect_daejeon(target, key, lock_conn)
            with self.db.connect(autocommit=True) as record_conn:
                recorder = FetchRecorder(record_conn)
                previous = self.client.on_attempt
                self.client.on_attempt = recorder
                try:
                    recorder.start(target.kind, key, REALTIME_NORMALIZER[target.kind])
                    if target.kind == "positions":
                        collection = self.client.bus_locations(target.external_id, self.city_code, rows=self.rows)
                    else:
                        collection = self.client.arrivals(target.external_id, self.city_code, rows=self.rows)
                    batch = recorder.finish(collection)
                finally:
                    self.client.on_attempt = previous
                attempts = len(batch.attempts)
                if not batch.complete:
                    lock_conn.rollback()
                    reason = f"realtime_not_published: {_batch_failure(batch)}"
                    repository.mark_unpublished(record_conn, batch.ids, reason, as_partial=True)
                    action = "partial" if collection.status == "partial" else "failed"
                    return CollectOutcome(key, action, batch.batch_key, attempts, reason=reason)
                try:
                    pages = [(no, fid, items) for no, fid, items in batch.pages()]
                    if any(fid is None for _, fid, _ in pages):
                        raise RuntimeError("채택할 수 없는 페이지가 있음")
                    received = {a.fetch_id: None for a in batch.attempts}
                    collected = self._received_times(lock_conn, list(received))
                    versions = repository.current_static_versions(lock_conn)
                    version = str(versions[0]) if len(versions) == 1 else None
                    normalize = normalize_positions if target.kind == "positions" else normalize_arrivals
                    observations = normalize(lock_conn, target.external_id, pages, collected, version)
                    inserted = repository.insert_observations(lock_conn, observations)
                    published_at = repository.clock_now(lock_conn)
                    adopted = batch.adopted_ids()
                    if repository.mark_published(lock_conn, adopted, published_at) != len(adopted):
                        raise RuntimeError("공개 표시 행 수가 채택 페이지 수와 다름")
                    lock_conn.commit()
                except Exception as exc:
                    lock_conn.rollback()
                    message = self.db.redact(f"{type(exc).__name__}: {exc}")[:500]
                    repository.mark_unpublished(record_conn, batch.ids, f"realtime_publish_failed: {message}",
                                                as_partial=False)
                    return CollectOutcome(key, "error", batch.batch_key, attempts, reason=message)
                action = "published" if inserted else "published_empty"
                return CollectOutcome(key, action, batch.batch_key, attempts, inserted)
        finally:
            lock_conn.close()

    def _collect_daejeon(self, target: RealtimeTarget, key: str, lock_conn: psycopg.Connection) -> CollectOutcome:
        """대전시 도착정보 1회 조회 → api_fetches 기록(즉시 커밋) → 정상·빈 응답이면 관측과 공개를 한 트랜잭션에서 저장."""
        bus_stop_id = daejeon_bus_stop_id(target.external_id)
        if bus_stop_id is None or not repository.is_active_stop(lock_conn, target.external_id):
            lock_conn.rollback()
            return CollectOutcome(key, "failed", reason="활성 정류소가 아니거나 'DJB'+숫자 형식이 아니어서 대전시 BusStopID를 만들 수 없음")
        page = self.daejeon_client.fetch_arrivals(bus_stop_id)
        with self.db.connect(autocommit=True) as record_conn:
            row = FetchAttemptRow(
                batch_key=uuid4(), provider=DAEJEON_PROVIDER, api_kind="arrivals", target_key=key,
                request_params=dict(page.params), page_no=1, attempt_no=1,
                requested_at=parse_utc(page.requested_at), received_at=parse_utc(page.received_at),
                http_status=page.http_status, provider_code=page.provider_code, result_status=page.status,
                total_count=page.item_count, raw_body=page.body, normalizer_version=DAEJEON_ARRIVALS_NORMALIZER_VERSION,
                error_reason=page.error[:2000] if page.error else None,
            )
            fetch_id = repository.insert_fetch(record_conn, row)
            if page.status not in ("success", "empty"):
                lock_conn.rollback()
                action = "partial" if page.status == "partial" else "failed"
                return CollectOutcome(key, action, row.batch_key, 1, reason=f"realtime_not_published: {page.error}")
            try:
                versions = repository.current_static_versions(lock_conn)
                version = str(versions[0]) if len(versions) == 1 else None
                observations = normalize_daejeon_arrivals(lock_conn, target.external_id, bus_stop_id, fetch_id, page.items,
                                                          row.received_at, version)
                inserted = repository.insert_observations(lock_conn, observations)
                if repository.mark_published(lock_conn, [fetch_id], repository.clock_now(lock_conn)) != 1:
                    raise RuntimeError("공개 표시 행 수가 채택 페이지 수와 다름")
                lock_conn.commit()
            except Exception as exc:
                lock_conn.rollback()
                message = self.db.redact(f"{type(exc).__name__}: {exc}")[:500]
                repository.mark_unpublished(record_conn, [fetch_id], f"realtime_publish_failed: {message}",
                                            as_partial=False)
                return CollectOutcome(key, "error", row.batch_key, 1, reason=message)
        return CollectOutcome(key, "published" if inserted else "published_empty", row.batch_key, 1, inserted)

    @staticmethod
    def _received_times(conn: psycopg.Connection, fetch_ids: list[int]) -> dict[int, datetime]:
        rows = conn.execute("SELECT id, received_at FROM app.api_fetches WHERE id = ANY(%s)", (fetch_ids,)).fetchall()
        return {fid: received for fid, received in rows}


def latest_snapshot(conn: psycopg.Connection, target: RealtimeTarget, *, interval_seconds: float,
                    now: datetime | None = None, city_code: str = CITY_CODE) -> LatestSnapshot:
    """마지막 공개 배치 전체와 최신성, 마지막 시도 상태. 실패·부분 시도는 이전 공개본을 바꾸지 않는다."""
    now = now or utc_now()
    key = realtime_target_key(target, city_code)
    pages = repository.latest_published_pages(conn, target.kind, key)
    last = repository.last_attempt(conn, target.kind, key)
    summary = None
    failed = False
    if last is not None:
        published = repository.batch_is_published(conn, last["batch_key"])
        summary = AttemptSummary(last["batch_key"], last["page_no"], last["attempt_no"], last["result_status"],
                                 last["requested_at"], published, last["error_reason"])
        failed = not published and (last["result_status"] in ("error", "timeout", "partial")
                                    or last["error_reason"] is not None)
    if not pages:
        return LatestSnapshot(key, target.kind, target.external_id, "no_data", None, None, None, None, None, 0, [],
                              summary, failed, NOT_PROVIDED[target.kind])
    collected = min(p["received_at"] for p in pages)
    age = (now - collected).total_seconds()
    observations = repository.observations_for_fetches(conn, [p["id"] for p in pages])
    content = "items" if observations else "empty"
    state = "fresh" if age < interval_seconds else "stale"
    return LatestSnapshot(key, target.kind, target.external_id, state, content, pages[0]["batch_key"],
                          pages[0]["published_at"], collected, age, len(pages), observations, summary, failed,
                          NOT_PROVIDED[target.kind])


def merge_targets(groups: Iterable[Iterable[RealtimeTarget]]) -> list[RealtimeTarget]:
    """여러 출처의 대상을 (kind, external_id)로 합치고 출처를 모은다. 같은 조회 조건은 한 번만 수집한다."""
    merged: dict[tuple[str, str], set[str]] = {}
    for group in groups:
        for target in group:
            merged.setdefault((target.kind, target.external_id), set()).update(target.sources)
    return [RealtimeTarget(kind, ext, frozenset(sources)) for (kind, ext), sources in sorted(merged.items())]
