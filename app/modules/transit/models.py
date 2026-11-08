"""교통정보 테이블(app 스키마)에 저장하는 행의 형식. ORM 없이 repository가 SQL로 읽고 쓴다.

적용 DDL: migrations/01_transit_core_supabase.sql(대상 DB에 적용된 파일의 사본). 외부 응답 형식과 구분한다.
외부 ID는 문자열이며, 좌표는 검증을 통과한 WGS84 값만 담고 미확인은 None이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

# api_fetches.result_status·api_kind 허용값(적용 DDL CHECK와 같다).
RESULT_STATUSES = ("success", "empty", "partial", "error", "timeout")
API_KINDS = ("stops", "routes", "route_stops", "positions", "arrivals")
STATIC_API_KINDS = ("stops", "routes", "route_stops")


@dataclass(frozen=True)
class FetchAttemptRow:
    """api_fetches 1행 = 실제 외부 HTTP 시도 1건."""

    batch_key: UUID
    provider: str
    api_kind: str
    target_key: str
    request_params: dict[str, str]
    page_no: int
    attempt_no: int
    requested_at: datetime
    received_at: datetime | None
    http_status: int | None
    provider_code: str | None
    result_status: str
    total_count: int | None
    raw_body: str | None
    normalizer_version: str
    error_reason: str | None = None


@dataclass(frozen=True)
class StopRow:
    external_id: str
    name: str | None
    stop_no: str | None
    lat: float | None
    lon: float | None
    source_fetch_id: int | None
    # station_list: 정류소 목록, route_stops: 정류소 목록에 없고 노선 경유 목록에서만 확인(시외 경유 등)
    origin: str


@dataclass(frozen=True)
class RouteRow:
    external_id: str
    route_no: str | None
    route_type: str | None
    start_name: str | None
    end_name: str | None
    service_info: dict[str, Any]
    source_fetch_id: int | None


@dataclass(frozen=True)
class RouteStopRow:
    route_external_id: str
    pattern_key: str
    direction_code: str | None
    seq: int
    source_node_order: str
    stop_external_id: str
    source_fetch_id: int | None


@dataclass(frozen=True)
class ObservationRow:
    """transit_observations의 position·arrival 1행. 원본 호출(fetch_id)과 응답 안 위치(item_index)가 필수다."""

    fetch_id: int
    item_index: int
    kind: str
    collected_at: datetime
    route_id: int | None = None
    stop_id: int | None = None
    pattern_key: str | None = None
    stop_seq: int | None = None
    vehicle_no: str | None = None
    run_key: str | None = None
    lat: float | None = None
    lon: float | None = None
    remaining_seconds: int | None = None
    eta_basis_at: datetime | None = None
    predicted_arrival_at: datetime | None = None
    source_generated_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)
