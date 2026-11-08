"""정적·실시간 교통정보의 모듈 간 데이터 형식.

최신성(state)·내용(content)·마지막 시도(last_attempt)를 따로 두어 최신/오래됨, 정상 빈 결과, 원천 미제공, 실패를
서로 대신하지 않는다. 시각은 모두 UTC aware datetime이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

RealtimeKind = Literal["positions", "arrivals"]
# fresh: 마지막 공개 배치가 수집 간격 이내, stale: 공개 배치는 있으나 간격을 넘김, no_data: 공개 배치 없음
Freshness = Literal["fresh", "stale", "no_data"]
# items: 항목이 있는 공개 배치, empty: 정상 빈 응답을 공개한 빈 스냅샷
Content = Literal["items", "empty"]


@dataclass(frozen=True)
class RealtimeTarget:
    """실시간 수집 대상 하나. 같은 kind·external_id는 출처가 여러 개여도 한 대상으로 합친다."""

    kind: RealtimeKind
    external_id: str
    sources: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AttemptSummary:
    batch_key: UUID
    page_no: int
    attempt_no: int
    result_status: str
    requested_at: datetime
    published: bool
    error_reason: str | None


@dataclass
class LatestSnapshot:
    """대상의 최신 공개 배치 전체와 마지막 시도 상태."""

    target_key: str
    kind: RealtimeKind
    external_id: str
    state: Freshness
    content: Content | None
    batch_key: UUID | None
    published_at: datetime | None
    # 공개 배치에서 가장 이른 페이지의 서버 수신 시각. 원천 관측·생성 시각이 아니다.
    collected_at: datetime | None
    age_seconds: float | None
    pages: int
    observations: list[dict[str, Any]]
    last_attempt: AttemptSummary | None
    # 마지막 시도가 실패·부분 수집이라 현재 값이 그 이전 공개본인 경우 True
    last_attempt_failed: bool
    # 이 응답 종류에서 원천이 제공하지 않는 항목(빈 값이 아니라 미제공)
    not_provided: tuple[str, ...]


@dataclass
class CollectOutcome:
    """대상 1회 처리 결과. reused·in_progress는 외부 호출 없이 끝난 경우다."""

    target_key: str
    action: Literal["published", "published_empty", "reused", "in_progress", "failed", "partial", "error"]
    batch_key: UUID | None = None
    http_attempts: int = 0
    observations: int = 0
    reason: str | None = None


@dataclass
class StaticProblem:
    """정적 공개를 보류하게 만든 문제. target_key가 없으면 실행 전체의 문제다."""

    code: str
    message: str
    target_key: str | None = None


@dataclass
class StaticImportReport:
    status: Literal["published", "held", "skipped_locked"] = "held"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    data_version: UUID | None = None
    published_at: datetime | None = None
    problems: list[StaticProblem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)
    loaded: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    calls: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NearbyStop:
    """저장 좌표로 계산한 미터 반경 안 정류소. 직선(측지) 거리이며 보행 가능·도보시간이 아니다."""

    stop_id: int
    external_id: str
    name: str | None
    stop_no: str | None
    distance_m: float
    lat: float
    lon: float


@dataclass(frozen=True)
class StaticStop:
    stop_id: int
    external_id: str
    name: str | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class StaticPatternStop:
    seq: int
    stop_external_id: str
    direction_code: str | None


@dataclass(frozen=True)
class StaticPattern:
    route_external_id: str
    pattern_key: str
    stops: tuple[StaticPatternStop, ...]


@dataclass(frozen=True)
class StaticRoute:
    route_id: int
    external_id: str
    route_no: str | None
    route_type: str | None


@dataclass(frozen=True)
class StaticNetwork:
    """한 REPEATABLE READ 트랜잭션에서 읽은 현재 정적 공개본. 교체 전후 버전을 섞지 않는다."""

    data_version: UUID
    stops: tuple[StaticStop, ...]
    patterns: tuple[StaticPattern, ...]
    routes: tuple[StaticRoute, ...] = ()
