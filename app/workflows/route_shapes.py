"""버스 노선 지도 형상 수집(ODsay) — 호출 예산 안에서 노선 ID 연결 → 형상 묶음 조회 → 정류소 대응 → 저장.

ODsay 일일 호출 한도가 작으므로(2026-11-12 사용자 확인: 30건 이하) 한 번에 끝내지 않고, 실행할 때마다 남은 예산만큼 진행한다.
진행 상태는 app.routes.shape_info에 있어 다음 실행이 이어서 한다.

1. 노선 ID 연결(첫 글자): 제공처 노선 ID가 없는 노선을 노선번호 첫 글자로 묶어 searchBusLane을 글자당 1회 호출한다
   (busNo는 앞부분 일치 검색). 응답의 localBusID가 우리 노선 ID(TAGO DJB…)와 같은 항목만 연결하고, 없으면 not_found로 남긴다
   (형상 사유 shape_route_not_in_provider). not_found는 갱신 간격이 지나면 다시 찾는다.
2. 형상 조회: 연결된 노선 중 현재 패턴의 형상이 없거나 갱신 간격이 지난 노선을 한 요청(loadLane)에 최대 batch_size개까지
   넣어 받는다(호출 수를 아끼려고 최대한 넣는다). 요청이 실패하면 반으로 나눠 다시 시도한다. 패턴마다 정류소를 선 위에
   대응시켜(route_shapes_api.build_pattern_shape) 저장한다. 응답 lane에는 노선 ID가 없어 요청 순서로 짝짓고 정류소 대응으로
   확인한다. 선 개수가 요청과 다르거나 순서대로 맞지 않는 노선은 남은 선 중 정류소가 대응되는 선을 찾아 추가 호출 없이
   짝짓고, 그래도 남은 노선만 1개씩 다시 받는다.
3. 노선 ID 연결(노선번호 전체): 1의 응답이 요청한 최대 개수만큼 꽉 차서 잘렸을 수 있을 때만, 그 글자에서 못 찾은 노선을
   노선번호 전체로 한 번 더 검색한다. 새로 연결된 노선은 2를 반복한다.
4. 호출 실패(오류·시간 초과)가 나면 그 실행을 멈춘다. 한도 초과일 수 있어 남은 예산을 더 쓰지 않는다.
5. 예산이 모자랄 때: 받을 형상이 있으면 형상 요청 1회분을 남기고 노선 검색을 멈춘다(연결만 하고 형상을 못 받으면
   지도에 달라지는 것이 없다). refresh_all이면 갱신 간격과 무관하게 연결된 노선의 형상을 모두 다시 받는다.

모든 HTTP 시도는 app.api_fetches에 즉시 기록한다(provider=odsay). 적용 DDL의 api_kind 허용값에 형상용 값이 없어
'routes'를 쓴다. 최근 24시간의 이 기록 수가 일일 한도 확인 기준이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

import psycopg

from app.db.session import Database, advisory_key
from app.integrations import route_shapes_api as route_shapes
from app.integrations.odsay_api import OdsayCall, OdsayClient
from app.modules.transit import repository
from app.modules.transit.models import FetchAttemptRow

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

PROVIDER = "odsay"
NORMALIZER_VERSION = f"odsay-shapes/1;{route_shapes.STOP_MATCH_METHOD}"
API_KIND = "routes"
LOCK_NAME = "route-shapes:collect"
# 노선 검색 1회에 받을 최대 결과 수. 대전은 첫 글자당 최대 25개 노선이라 여유가 크다. 응답이 이 수만큼 차면 잘렸을 수 있다.
SEARCH_DISPLAY_COUNT = 200
SHAPE_NOT_PROVIDED_REASON = "shape_not_provided"
# 형상 요청이 실패했을 때 이 크기보다 큰 묶음은 반으로 나눠 다시 시도한다. 이 크기 이하에서도 실패하면 실행을 멈춘다.
MIN_SPLIT_BATCH = 4
QUOTA_EXCEEDED_CODES = ("429",)
USAGE_WINDOW = timedelta(hours=24)


class ShapeConfigError(RuntimeError):
    """형상 수집 설정이 부족하다(제공처·호출 한도)."""


@dataclass
class ShapeReport:
    """수집 1회의 결과. stopped: None(할 일을 모두 처리) / budget_exhausted / call_failed / locked."""

    provider: str
    started_at: datetime
    max_calls: int
    calls: list[dict[str, Any]] = field(default_factory=list)
    routes: int = 0
    matched: int = 0
    not_found: list[str] = field(default_factory=list)
    shapes_saved: int = 0
    shape_problems: list[dict[str, Any]] = field(default_factory=list)
    pending_mapping: int = 0
    pending_shapes: int = 0
    stopped: str | None = None


class ShapeStore(Protocol):
    """수집 흐름이 쓰는 저장소. 운영은 DbShapeStore, 단위 시험은 메모리 구현을 쓴다."""

    def routes(self) -> list[dict[str, Any]]: ...

    def stop_points(self, route_ids: Iterable[int]) -> dict[int, dict[str, list[tuple[int, float | None, float | None]]]]: ...

    def save_provider(self, route_id: int, provider: dict[str, Any]) -> None: ...

    def save_patterns(self, route_id: int, provider: dict[str, Any], patterns: dict[str, Any]) -> None: ...

    def record_call(self, call: OdsayCall, target_key: str, total_count: int | None) -> None: ...


class DbShapeStore:
    """autocommit 연결로 호출 기록과 노선별 저장을 즉시 커밋한다(중간에 멈춰도 진행분이 남는다)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        if not conn.autocommit:
            raise ValueError("DbShapeStore에는 autocommit 연결이 필요합니다.")
        self.conn = conn

    def routes(self) -> list[dict[str, Any]]:
        return repository.shape_collection_routes(self.conn)

    def stop_points(self, route_ids: Iterable[int]) -> dict[int, dict[str, list[tuple[int, float | None, float | None]]]]:
        return repository.pattern_stop_points(self.conn, route_ids)

    def save_provider(self, route_id: int, provider: dict[str, Any]) -> None:
        repository.set_shape_provider(self.conn, route_id, provider)

    def save_patterns(self, route_id: int, provider: dict[str, Any], patterns: dict[str, Any]) -> None:
        repository.save_shape_patterns(self.conn, route_id, provider, patterns)

    def record_call(self, call: OdsayCall, target_key: str, total_count: int | None) -> None:
        repository.insert_fetch(self.conn, FetchAttemptRow(
            batch_key=uuid4(), provider=PROVIDER, api_kind=API_KIND, target_key=target_key,
            request_params=dict(call.params), page_no=1, attempt_no=1, requested_at=call.requested_at,
            received_at=call.received_at, http_status=call.http_status, provider_code=call.provider_code,
            result_status=call.status, total_count=total_count, raw_body=call.body,
            normalizer_version=NORMALIZER_VERSION, error_reason=call.error[:2000] if call.error else None))


class RouteShapeCollector:
    def __init__(self, store: ShapeStore, client: OdsayClient, *, city_code: str, batch_size: int,
                 refresh_seconds: float, refresh_all: bool = False,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.client = client
        self.city_code = city_code
        self.batch_size = max(1, batch_size)
        self.refresh = timedelta(seconds=refresh_seconds)
        self.refresh_all = refresh_all
        self.clock = clock or utc_now
        self._fetched: set[int] = set()      # 이번 실행에서 형상을 저장한 노선(refresh_all이 같은 노선을 두 번 받지 않게)

    def run(self, max_calls: int) -> ShapeReport:
        now = self.clock()
        report = ShapeReport(provider=PROVIDER, started_at=now, max_calls=max(0, max_calls))
        self._fetched = set()
        routes = self.store.routes()
        report.routes = len(routes)
        points: dict[int, dict[str, Any]] = {}
        # 첫 글자 검색으로 대부분을 연결해 형상을 먼저 받고, 빠진 노선은 노선번호 전체 검색 뒤에 받는다.
        for map_routes in (self._map_by_prefix, self._map_by_number):
            if report.stopped is None:
                map_routes(routes, report, now)
            matched = [r for r in routes if _is_matched(r)]
            points.update(self.store.stop_points(r["id"] for r in matched if r["id"] not in points))
            if report.stopped is None:
                self._collect_shapes(matched, points, report, now)
        report.pending_mapping = sum(1 for r in routes if self._needs_mapping(r, now))
        report.pending_shapes = sum(1 for r in routes
                                    if _is_matched(r) and self._needs_shape(r, points.get(r["id"], {}), now))
        if report.stopped is None and (report.pending_mapping or report.pending_shapes):
            report.stopped = "budget_exhausted"      # 형상 요청분을 남기려고 검색을 멈춘 경우
        return report

    # ------------------------------------------------------------ 노선 ID 연결

    def _needs_mapping(self, route: dict[str, Any], now: datetime) -> bool:
        provider = route["provider"]
        if not provider or provider.get("name") != PROVIDER:
            return True
        if _is_matched(route):
            return False
        return _awaits_number_search(route) or _older_than(provider.get("checked_at"), now - self.refresh)

    def _map_by_prefix(self, routes: list[dict[str, Any]], report: ShapeReport, now: datetime) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for route in routes:
            if self._needs_mapping(route, now) and not _awaits_number_search(route):
                groups.setdefault(_route_no(route)[:1], []).append(route)
        # 노선번호가 없는 노선은 검색할 수 없다.
        for route in groups.pop("", []):
            self._set_provider(route, None, "number", now, report)
        for prefix in sorted(groups, key=lambda p: (-len(groups[p]), p)):
            # 받을 형상이 있으면 형상 요청 1회분은 남긴다. 남은 검색은 다음 실행이 이어서 한다.
            if self._shape_request_expected(routes) and len(report.calls) + 1 >= report.max_calls:
                return
            result = self._search(prefix, report)
            if result is None:
                return
            found, truncated = result
            for route in groups[prefix]:
                # 응답이 잘렸을 수 있고 노선번호가 첫 글자보다 길면 번호 전체 검색을 남긴다. 아니면 여기서 확정한다.
                searched = "prefix" if truncated and _route_no(route) != prefix else "number"
                self._set_provider(route, found.get(route["external_id"]), searched, now, report)

    def _shape_request_expected(self, routes: list[dict[str, Any]]) -> bool:
        """연결된 노선 중 형상이 아직 없거나(refresh_all이면 이번 실행에서 아직 안 받은) 노선이 있는지."""
        return any(_is_matched(r) and (not r["patterns"] or (self.refresh_all and r["id"] not in self._fetched))
                   for r in routes)

    def _map_by_number(self, routes: list[dict[str, Any]], report: ShapeReport, now: datetime) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for route in routes:
            if _awaits_number_search(route):
                groups.setdefault(_route_no(route), []).append(route)
        for number in sorted(groups):
            result = self._search(number, report)
            if result is None:
                return
            for route in groups[number]:
                self._set_provider(route, result[0].get(route["external_id"]), "number", now, report)

    def _search(self, bus_no: str, report: ShapeReport) -> tuple[dict[str, str], bool] | None:
        """노선번호가 bus_no로 시작하는 노선의 ({지역 노선 ID: ODsay 노선 ID}, 잘렸을 수 있는지). 예산 소진·실패면 None."""
        if not self._spend(report):
            return None
        search = self.client.search_bus_lanes(bus_no, city_code=self.city_code, display_count=SEARCH_DISPLAY_COUNT)
        self._record(search.call, f"{PROVIDER}:searchBusLane?busNo={bus_no}&CID={self.city_code}",
                     len(search.lanes), report)
        if not search.call.ok:
            report.stopped = "call_failed"
            return None
        found: dict[str, str] = {}
        for lane in search.lanes:
            for local_id in (lane.local_bus_id or "").split("/"):
                if local_id.strip():
                    found.setdefault(local_id.strip(), lane.bus_id)
        return found, len(search.lanes) >= SEARCH_DISPLAY_COUNT

    def _set_provider(self, route: dict[str, Any], bus_id: str | None, searched: str, now: datetime,
                      report: ShapeReport) -> None:
        """검색 결과를 노선에 남긴다. searched가 prefix면 번호 전체 검색이 남았고, number면 이번 검색으로 확정한 것이다."""
        provider = {"name": PROVIDER, "status": "matched" if bus_id else "not_found", "route_id": bus_id,
                    "searched": searched, "checked_at": _iso(now)}
        self.store.save_provider(route["id"], provider)
        route["provider"] = provider
        if bus_id:
            report.matched += 1
        elif searched == "number":
            report.not_found.append(route["external_id"])

    # ------------------------------------------------------------ 형상 조회·저장

    def _needs_shape(self, route: dict[str, Any], patterns: dict[str, Any], now: datetime) -> bool:
        if self.refresh_all and patterns and route["id"] not in self._fetched:
            return True
        for key in patterns:
            entry = route["patterns"].get(key)
            if not entry or entry.get("source") != PROVIDER or _older_than(entry.get("fetched_at"), now - self.refresh):
                return True
        return False

    def _collect_shapes(self, matched: list[dict[str, Any]], points: dict[int, dict[str, Any]], report: ShapeReport,
                        now: datetime) -> None:
        due = [r for r in matched if self._needs_shape(r, points.get(r["id"], {}), now)]

        def age(route: dict[str, Any]) -> str:
            times = [str((route["patterns"].get(key) or {}).get("fetched_at") or "") for key in points[route["id"]]]
            return min(times) if times else ""

        due.sort(key=lambda r: (age(r), r["route_no"] or "", r["id"]))   # 형상이 없는 노선 먼저, 다음은 오래된 순서
        single = [r for r in due if r["provider"].get("lane_batch_failed_at")]
        grouped = [r for r in due if not r["provider"].get("lane_batch_failed_at")]
        queue = [grouped[i:i + self.batch_size] for i in range(0, len(grouped), self.batch_size)]
        queue += [[r] for r in single]
        while queue:
            batch = queue.pop(0)
            if not self._spend(report):
                return
            ids = [str(r["provider"]["route_id"]) for r in batch]
            shapes = self.client.load_lanes(ids)
            self._record(shapes.call, f"{PROVIDER}:loadLane?busID={','.join(ids)}", len(shapes.lines), report)
            if shapes.call.status in ("error", "timeout"):
                quota = shapes.call.http_status == 429 or shapes.call.provider_code in QUOTA_EXCEEDED_CODES
                if quota or len(batch) <= MIN_SPLIT_BATCH:
                    report.stopped = "call_failed"
                    return
                half = len(batch) // 2          # 요청이 너무 컸을 수 있다. 반으로 나눠 먼저 다시 시도한다.
                queue[:0] = [batch[:half], batch[half:]]
                continue
            in_order = shapes.call.status == "success"
            unresolved = self._assign_lines(batch, shapes.lines, in_order, points, report, now)
            queue.extend([route] for route in unresolved)     # 짝을 찾지 못한 노선만 1개씩 다시 받는다.

    def _assign_lines(self, batch: list[dict[str, Any]], lines: list[list[list[float]]], in_order: bool,
                      points: dict[int, dict[str, Any]], report: ShapeReport, now: datetime) -> list[dict[str, Any]]:
        """응답의 선을 노선에 짝지어 저장한다. 단독 재조회가 필요한 노선을 돌려준다."""
        if len(batch) == 1:
            # 단독 조회: 받은 선이 그 노선의 선이다. 대응 실패·미제공도 그대로 확정해 저장한다.
            route = batch[0]
            self._store(route, self._entries(route, lines[0] if len(lines) == 1 else None, points, now), report)
            return []
        unresolved: list[dict[str, Any]] = []
        spare: list[list[list[float]]] = []
        if in_order:
            for route, line in zip(batch, lines):
                entries = self._entries(route, line, points, now)
                if _aligned(entries):
                    self._store(route, entries, report)
                else:
                    unresolved.append(route)
                    spare.append(line)
        else:
            unresolved, spare = list(batch), list(lines)
        # 순서로 짝짓지 못한 노선: 남은 선 중 정류소가 대응되는 선을 찾는다(범위가 맞는 선만 대응을 계산).
        boxes = [_bounds(line) for line in spare]
        used = [False] * len(spare)
        waiting: list[dict[str, Any]] = []
        for route in unresolved:
            stops = [stop for rows in points[route["id"]].values() for stop in rows]
            for i, line in enumerate(spare):
                if used[i] or not _may_fit(boxes[i], stops):
                    continue
                entries = self._entries(route, line, points, now)
                if _aligned(entries):
                    used[i] = True
                    self._store(route, entries, report)
                    break
            else:
                provider = {**route["provider"], "lane_batch_failed_at": _iso(now)}
                self.store.save_provider(route["id"], provider)
                route["provider"] = provider
                waiting.append(route)
        return waiting

    def _entries(self, route: dict[str, Any], line: list[list[float]] | None, points: dict[int, dict[str, Any]],
                 now: datetime) -> dict[str, Any]:
        """노선의 현재 패턴별 형상 항목. line이 None이면 제공처가 형상을 주지 않은 것이다."""
        bus_id = str(route["provider"]["route_id"])
        entries: dict[str, Any] = {}
        for key, stop_rows in points[route["id"]].items():
            if line is None:
                entries[key] = {"status": "unavailable", "geometry": None, "source": PROVIDER,
                                "source_route_id": bus_id, "version": None, "fetched_at": _iso(now),
                                "reason": SHAPE_NOT_PROVIDED_REASON, "stop_vertex_index": {}}
                continue
            stops = [route_shapes.StopPoint(seq=seq, lon=lon, lat=lat) for seq, lon, lat in stop_rows
                     if lon is not None and lat is not None]
            entries[key] = route_shapes.build_pattern_shape(line, stops, source=PROVIDER, source_route_id=bus_id,
                                                            fetched_at=_iso(now))
        return entries

    def _store(self, route: dict[str, Any], entries: dict[str, Any], report: ShapeReport) -> None:
        provider = {k: v for k, v in route["provider"].items() if k != "lane_batch_failed_at"}
        self.store.save_patterns(route["id"], provider, entries)
        self._fetched.add(route["id"])
        route["provider"] = provider
        route["patterns"] = {key: {"source": e["source"], "status": e["status"], "fetched_at": e["fetched_at"]}
                             for key, e in entries.items()}
        for key, entry in entries.items():
            if entry["status"] == "available":
                report.shapes_saved += 1
            if entry["status"] != "available" or entry["stop_match"]["unmatched_seqs"]:
                report.shape_problems.append({
                    "route": route["external_id"], "route_no": route["route_no"], "pattern_key": key,
                    "status": entry["status"], "reason": entry.get("reason"), "stop_match": entry.get("stop_match")})

    # ------------------------------------------------------------ 호출 예산·기록

    def _spend(self, report: ShapeReport) -> bool:
        if len(report.calls) >= report.max_calls:
            report.stopped = "budget_exhausted"
            return False
        return True

    def _record(self, call: OdsayCall, target_key: str, total_count: int | None, report: ShapeReport) -> None:
        report.calls.append({"operation": call.operation, "target": target_key, "status": call.status,
                             "http_status": call.http_status, "error": call.error})
        self.store.record_call(call, target_key, total_count)
        logger.info("형상 수집 호출 %s: %s", target_key, call.status)


def remaining_daily_calls(conn: psycopg.Connection, daily_limit: int, now: datetime) -> int:
    """일일 한도에서 최근 24시간 호출 수를 뺀 값(0 이상)."""
    return max(0, daily_limit - repository.provider_call_count(conn, PROVIDER, now - USAGE_WINDOW))


def collect(db: Database, settings: Settings, client: OdsayClient, *, max_calls: int | None = None,
            refresh_all: bool = False, clock: Callable[[], datetime] | None = None) -> ShapeReport:
    """형상 수집 1회. max_calls를 생략하면 일일 한도의 남은 호출 수만큼, 지정하면 그 수와 남은 한도 중 작은 쪽만큼 쓴다.

    refresh_all이면 갱신 간격과 무관하게 연결된 노선의 형상을 모두 다시 받는다(한 요청에 최대한 넣는다).
    """
    if route_shapes.configured_provider(settings.route_shape_provider) != PROVIDER:
        raise ShapeConfigError("BACKEND_ROUTE_SHAPE_PROVIDER가 odsay가 아닙니다.")
    if max_calls is None and settings.odsay_daily_call_limit is None:
        raise ShapeConfigError("BACKEND_ODSAY_DAILY_CALL_LIMIT이 비어 있습니다. 확인한 일일 한도를 넣거나 호출 수를 지정하세요.")
    clock = clock or utc_now
    conn = db.connect(autocommit=True)
    try:
        now = clock()
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (advisory_key(LOCK_NAME),)).fetchone()[0]:
            return ShapeReport(provider=PROVIDER, started_at=now, max_calls=0, stopped="locked")
        budget = max_calls
        if settings.odsay_daily_call_limit is not None:
            remaining = remaining_daily_calls(conn, settings.odsay_daily_call_limit, now)
            budget = remaining if budget is None else min(budget, remaining)
        collector = RouteShapeCollector(
            DbShapeStore(conn), client, city_code=settings.odsay_city_code, batch_size=settings.odsay_lane_batch_size,
            refresh_seconds=settings.route_shape_refresh_interval_seconds, refresh_all=refresh_all, clock=clock)
        return collector.run(budget or 0)
    finally:
        conn.close()


def _is_matched(route: dict[str, Any]) -> bool:
    provider = route["provider"]
    return bool(provider and provider.get("name") == PROVIDER and provider.get("status") == "matched"
                and provider.get("route_id"))


def _aligned(entries: dict[str, Any]) -> bool:
    return any(entry["status"] == "available" for entry in entries.values())


def _bounds(line: list[list[float]]) -> tuple[float, float, float, float]:
    """선의 범위(경도·위도 최소·최대)에 정류소 대응 허용 거리만큼 여유를 둔 값."""
    margin = route_shapes.STOP_MATCH_MAX_METERS / 80_000.0     # 대전 위도에서 경도 1도 ≈ 89 km, 위도 1도 ≈ 111 km
    lons, lats = [p[0] for p in line], [p[1] for p in line]
    return min(lons) - margin, min(lats) - margin, max(lons) + margin, max(lats) + margin


def _may_fit(bounds: tuple[float, float, float, float], stops: list[tuple[int, float | None, float | None]]) -> bool:
    """정류소 대부분이 선의 범위 안에 있는지(대응 계산 전에 다른 노선의 선을 싸게 걸러낸다)."""
    located = [(lon, lat) for _, lon, lat in stops if lon is not None and lat is not None]
    inside = sum(1 for lon, lat in located if bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3])
    return bool(located) and inside >= route_shapes.PATTERN_MATCH_MIN_SHARE * len(located)


def _route_no(route: dict[str, Any]) -> str:
    return (route["route_no"] or "").strip()


def _awaits_number_search(route: dict[str, Any]) -> bool:
    """첫 글자 검색 응답이 잘렸을 수 있어 노선번호 전체 검색이 남은 노선."""
    provider = route["provider"]
    return bool(provider and provider.get("name") == PROVIDER and provider.get("status") == "not_found"
                and provider.get("searched") != "number" and len(_route_no(route)) > 1)


def _older_than(timestamp: Any, threshold: datetime) -> bool:
    """ISO 시각이 기준보다 오래됐는지. 값이 없거나 해석할 수 없으면 오래된 것으로 본다."""
    try:
        value = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value < threshold


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
