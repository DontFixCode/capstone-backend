"""혼잡도 입력·집계·조회의 업무 흐름.

- 입력(import_files): 플랫폼 원본 파일을 해석·검증하고 노선과 정류소를 내부 값에 대응시켜 원본 테이블에 누적한다.
  파일 하나라도 검증에 실패하면 그 파일은 한 행도 저장하지 않는다(다른 파일은 그대로 처리). 같은 노선·일자·시간·순번이
  이미 있으면 건너뛰고 건너뛴 수와 그중 값이 다른 수를 알려 준다. execute가 아니면 저장하지 않고 결과만 미리 보여 준다.
- 노선 대응: 노선 칸의 노선번호로 찾고, 같은 번호가 여럿이면 괄호 안 기점~종점 이름으로 고른다. 그래도 하나로 정해지지
  않으면 그 파일을 거부한다.
- 정류소 대응: 원본의 (순번, 정류소 번호·이름) 목록을 그 노선의 현재 경유 순서에 순서를 지켜 맞춘다. 정류소 번호는 노선
  안에서만 비교한다(전체 정류소에는 번호가 없거나 겹치는 정류소가 있다). 맞지 않은 순번은 stop_id를 비워 두고 목록으로 알린다.
- 집계(aggregate_route): 집계 시점 기준 직전 n개월의 원본만 읽어 현재 패턴의 프로필을 만들고 한 트랜잭션에서 노선의 기존
  행을 교체한다. 자료가 없거나 정류소 대응이 기준 미만이면 기존 행을 그대로 둔다. 시간당 통과 횟수는 같은 기간의 노선
  위치 관측에서 실제로 지나간 차량을 세어 쓰고(관측이 모자란 요일유형·시간대는 배차간격), 그래서 원본이 있는 노선은
  워커가 위치를 상시 수집한다(position_targets).
- 조회(Lookup): 검색·도착 목록이 쓰는 읽기 전용 묶음. 테이블이 아직 적용되지 않은 DB에서는 모두 미확인으로 답한다.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from app.core.config import Settings
from app.db.session import Database
from app.modules.congestion import equilibrium, levels, repository
from app.modules.congestion.aggregation import (
    KST,
    AggregateResult,
    CountRow,
    ObservedTrips,
    VehiclePoint,
    align_stops,
    build_profiles,
    day_type,
    observed_trips,
    timetable_trips_per_hour,
    window_start,
)
from app.modules.congestion.source import ParsedFile, SourceFileError, SourceRow, parse_export, route_parts
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import realtime_target_key

logger = logging.getLogger(__name__)


class CongestionConfigError(RuntimeError):
    """집계 설정이 없다. 메시지에는 설정 이름만 담는다."""


# ---------------------------------------------------------------- 입력

def _stop_key(stop_no: str | None, name: str | None) -> str | None:
    """정류소 대응에 쓰는 키: 정류소 번호가 있으면 번호, 없으면 이름."""
    if stop_no:
        return f"no:{str(stop_no).strip()}"
    return f"name:{name.strip()}" if name and name.strip() else None


def resolve_route(conn: psycopg.Connection, label: str) -> tuple[dict[str, Any] | None, str | None]:
    """노선 칸의 글자로 내부 노선을 찾는다. (노선, 실패 사유)."""
    number, start, end = route_parts(label)
    found = repository.routes_by_number(conn, number)
    if len(found) > 1 and start and end:
        named = [r for r in found if (r["start_name"] or "").strip() == start and (r["end_name"] or "").strip() == end]
        found = named or found
    if not found:
        return None, f"노선 '{label}'을 현재 정적 정보에서 찾지 못했습니다."
    if len(found) > 1:
        return None, f"노선 '{label}'에 해당하는 노선이 {len(found)}개여서 하나로 정할 수 없습니다."
    return found[0], None


def _map_stops(rows: Sequence[SourceRow], patterns: dict[str, list[dict[str, Any]]]
               ) -> tuple[dict[int, int], str | None, list[dict[str, Any]]]:
    """(원본 순번 → 내부 정류소 ID, 맞춘 패턴, 대응하지 못한 원본 정류장 목록). 가장 많이 맞는 패턴을 쓴다."""
    source: dict[int, SourceRow] = {}
    for row in rows:
        source.setdefault(row.stop_order, row)
    ordered = sorted(source.items())
    best: tuple[dict[int, int], str | None] = ({}, None)
    for key, stops in sorted(patterns.items()):
        mapping = align_stops([(order, _stop_key(row.stop_no, row.stop_name)) for order, row in ordered],
                              [(stop["seq"], _stop_key(stop["stop_no"], stop["name"])) for stop in stops])
        by_seq = {stop["seq"]: stop["stop_id"] for stop in stops}
        resolved = {order: by_seq[seq] for order, seq in mapping.items()}
        if len(resolved) > len(best[0]):
            best = (resolved, key)
    unmatched = [{"order": order, "stop_no": row.stop_no, "name": row.stop_name}
                 for order, row in ordered if order not in best[0]]
    return best[0], best[1], unmatched


def _coverage(rows: Sequence[SourceRow]) -> dict[str, Any]:
    """기간 중 빠진 일자와 일자별 시간대 수(시간대가 빠진 날이 있는지 확인하도록 알려 준다)."""
    days = sorted({row.service_date for row in rows})
    present = set(days)
    missing = [(days[0] + timedelta(days=i)).isoformat() for i in range((days[-1] - days[0]).days + 1)
               if days[0] + timedelta(days=i) not in present]
    hours_by_day: dict[date, set[int]] = {}
    for row in rows:
        hours_by_day.setdefault(row.service_date, set()).add(row.hour)
    hour_counts = Counter(len(hours) for hours in hours_by_day.values())
    return {"first_date": days[0].isoformat(), "last_date": days[-1].isoformat(), "days": len(days),
            "missing_dates": missing, "hours": sorted({row.hour for row in rows}),
            "days_by_hour_count": {str(count): number for count, number in sorted(hour_counts.items())}}


def import_files(db: Database, files: Sequence[tuple[str, bytes]], *, execute: bool = False,
                 uploaded_by: UUID | None = None) -> dict[str, Any]:
    """원본 파일 여러 개를 입력한다. 반환: 파일별 결과와 영향을 받은 노선 ID."""
    report: dict[str, Any] = {"executed": execute, "files": [], "route_ids": []}
    touched: set[int] = set()
    for name, data in files:
        entry: dict[str, Any] = {"file": name, "status": "rejected", "reasons": []}
        report["files"].append(entry)
        try:
            parsed: ParsedFile = parse_export(data, file_name=name)
        except SourceFileError as exc:
            entry["reasons"] = exc.reasons
            continue
        by_route: dict[str, list[SourceRow]] = {}
        for row in parsed.rows:
            by_route.setdefault(row.route_label, []).append(row)
        with db.transaction() as conn:
            ready = repository.tables_ready(conn)
            if execute and not ready:
                entry["reasons"] = ["혼잡도 테이블이 없습니다(migrations/13_congestion.sql 미적용)."]
                continue
            resolved = {label: resolve_route(conn, label) for label in by_route}
            failures = [reason for _, reason in resolved.values() if reason]
            if failures:
                entry["reasons"] = failures
                continue
            entry.update(status="accepted" if execute else "valid", rows=len(parsed.rows),
                         checked_totals=parsed.checked_totals, warnings=list(parsed.warnings), routes=[])
            if not ready:
                entry["warnings"].append("혼잡도 테이블이 아직 없어 이미 있는 행과는 비교하지 못했습니다.")
            for label, rows in sorted(by_route.items()):
                route = resolved[label][0]
                stop_ids, pattern_key, unmatched = _map_stops(rows, repository.pattern_stops(conn, route["id"]))
                boarded = sum(row.board + row.transfer for row in rows)
                alighted = sum(row.alight for row in rows)
                item: dict[str, Any] = {
                    "route_label": label, "route_id": route["id"], "route_no": route["route_no"],
                    "external_id": route["external_id"], "rows": len(rows), **_coverage(rows),
                    "matched_pattern": pattern_key, "source_stops": len({row.stop_order for row in rows}),
                    "unmatched_stops": unmatched, "total_board": boarded, "alight": alighted,
                    "board_per_alight": round(boarded / alighted, 3) if alighted else None,
                }
                if execute:
                    payload = [(route["id"], row.service_date, row.hour, row.stop_order, row.stop_no, row.stop_name,
                                stop_ids.get(row.stop_order), row.board, row.alight, row.transfer) for row in rows]
                    inserted, skipped, differing = repository.insert_counts(conn, payload, source_file=name,
                                                                            uploaded_by=uploaded_by)
                else:
                    present = repository.existing_keys(conn, route["id"], min(r.service_date for r in rows),
                                                       max(r.service_date for r in rows)) if ready else {}
                    hits = [(row, present[(row.service_date, row.hour, row.stop_order)]) for row in rows
                            if (row.service_date, row.hour, row.stop_order) in present]
                    skipped = len(hits)
                    differing = sum(1 for row, old in hits if old != (row.board, row.alight, row.transfer))
                    inserted = len(rows) - skipped
                item.update(inserted=inserted, skipped_existing=skipped, skipped_with_different_values=differing)
                entry["routes"].append(item)
                touched.add(route["id"])
    report["route_ids"] = sorted(touched)
    return report


# ---------------------------------------------------------------- 집계

def _summary(route: dict[str, Any], result: AggregateResult, *, window: tuple[date, date], saved: int | None,
             pattern_key: str | None) -> dict[str, Any]:
    peak = max(result.profiles, key=lambda row: (row.flow_p80 if row.flow_p80 is not None else row.flow_mean),
               default=None)
    return {
        "route_id": route["id"], "route_no": route["route_no"], "status": result.status, "reason": result.reason,
        "pattern_key": pattern_key, "window": [window[0].isoformat(), window[1].isoformat()],
        "source_period": result.source_period, "days_used": result.days_used, "hours_skipped": result.hours_skipped,
        "matched_stops": result.matched_stops, "source_stops": result.source_stops,
        "pattern_stops": result.pattern_stops,
        "board_per_alight_median": None if result.alpha_median is None else round(result.alpha_median, 3),
        "profiles": len(result.profiles), "saved": saved,
        "trips": _trips_summary(result),
        "peak": None if peak is None else {
            "day_type": peak.day_type, "hour": peak.hour, "stop_seq": peak.stop_seq,
            "flow_mean": round(peak.flow_mean, 1),
            "flow_p80": None if peak.flow_p80 is None else round(peak.flow_p80, 1), "sample_days": peak.sample_days,
            "load_per_vehicle": None if not peak.trips_per_hour else round(
                (peak.flow_p80 if peak.flow_p80 is not None else peak.flow_mean) / peak.trips_per_hour, 1)},
    }


def _trips_summary(result: AggregateResult) -> dict[str, Any]:
    """통과 횟수의 근거별 칸 수와 요일유형·시간대별 대표값(구간 중앙값)."""
    by_source: dict[str, int] = {}
    cells: dict[tuple[str, int], list[tuple[str | None, float | None]]] = {}
    for row in result.profiles:
        by_source[row.trips_source or "unknown"] = by_source.get(row.trips_source or "unknown", 0) + 1
        cells.setdefault((row.day_type, row.hour), []).append((row.trips_source, row.trips_per_hour))
    by_hour: dict[str, dict[str, Any]] = {}
    for (kind, hour), values in sorted(cells.items()):
        known = sorted(value for _, value in values if value is not None)
        sources = {source or "unknown" for source, _ in values}
        by_hour.setdefault(kind, {})[f"{hour:02d}"] = {
            "source": "observed" if "observed" in sources else sorted(sources)[0],
            "median": None if not known else round(known[len(known) // 2], 2)}
    return {"cells_by_source": dict(sorted(by_source.items())), "by_hour": by_hour}


def position_observations(conn: psycopg.Connection, settings: Settings, route: dict[str, Any], pattern_key: str,
                          last_seq: int, window: tuple[date, date]) -> ObservedTrips:
    """집계 창 안의 노선 위치 관측으로 구간별·시간대별 실제 통과 횟수를 센다(DB만 읽음)."""
    first = datetime.combine(window[0], datetime.min.time(), tzinfo=KST)
    last = datetime.combine(window[1] + timedelta(days=1), datetime.min.time(), tzinfo=KST)
    snapshots, rows = repository.position_history(
        conn, route["id"], pattern_key, realtime_target_key(RealtimeTarget("positions", route["external_id"])),
        first, last)
    # 수집 간격의 두 배까지는 이어진 관측으로 본다(대상별 위상 맞춤으로 간격이 한 번 두 배가 될 수 있다).
    return observed_trips(snapshots, [VehiclePoint(at, vehicle, seq) for at, vehicle, seq in rows], last_seq,
                          max_gap_seconds=2 * settings.realtime_interval_seconds + 60,
                          holidays=settings.congestion_holidays, min_days=settings.congestion_min_sample_days)


def compute(route: dict[str, Any], patterns: dict[str, list[dict[str, Any]]], rows: Iterable[CountRow],
            settings: Settings, *, conn: psycopg.Connection | None = None,
            window: tuple[date, date] | None = None) -> tuple[AggregateResult, str | None]:
    """노선의 현재 패턴(하나여야 한다)에 대한 집계 결과. 패턴이 여럿이면 방향을 가를 근거가 원본에 없어 보류한다.

    conn과 window를 주면 같은 기간의 위치 관측으로 통과 횟수를 세고, 없으면 배차간격만 쓴다.
    """
    if len(patterns) != 1:
        reason = "route_has_no_pattern" if not patterns else "route_has_multiple_patterns"
        return AggregateResult("held", reason, []), None
    (pattern_key, stops), = patterns.items()
    observed = None
    if conn is not None and window is not None:
        observed = position_observations(conn, settings, route, pattern_key, max(stop["seq"] for stop in stops), window)
    result = build_profiles(rows, pattern_key, [(stop["seq"], stop["stop_id"]) for stop in stops],
                            service_info=route.get("service_info"), holidays=settings.congestion_holidays,
                            min_sample_days=settings.congestion_min_sample_days, observed=observed)
    return result, pattern_key


def position_targets(conn: psycopg.Connection) -> list[RealtimeTarget]:
    """승하차 원본이 있는 노선의 위치 수집 대상. 실제 통과 횟수를 세려면 운행 시간 내내 위치가 쌓여야 한다.

    테이블이 아직 없으면 빈 목록이다. 노선 하나에 TAGO 위치 조회가 수집 간격(기본 600초)마다 1건 든다.
    """
    if not repository.tables_ready(conn):
        return []
    source = frozenset({"congestion"})
    return [RealtimeTarget("positions", external_id, source)
            for external_id in repository.route_external_ids(conn, repository.routes_with_counts(conn))]


def aggregate_route(db: Database, settings: Settings, route_id: int, *, now: datetime,
                    execute: bool = False) -> dict[str, Any]:
    """노선 하나를 다시 집계한다. 계산에 성공했을 때만 기존 행을 교체한다(execute가 아니면 저장하지 않음)."""
    if settings.congestion_window_months is None:
        raise CongestionConfigError("BACKEND_CONGESTION_WINDOW_MONTHS가 비어 있습니다.")
    window = (window_start(now, settings.congestion_window_months), now.astimezone(KST).date())
    with db.transaction() as conn:
        route = repository.route_by_id(conn, route_id)
        if route is None:
            return {"route_id": route_id, "status": "held", "reason": "route_not_found"}
        result, pattern_key = compute(route, repository.pattern_stops(conn, route_id),
                                      repository.counts_in_window(conn, route_id, *window), settings,
                                      conn=conn, window=window)
        saved = None
        if execute and result.status == "computed":
            saved = repository.replace_profiles(conn, route_id, result.profiles)
        return _summary(route, result, window=window, saved=saved, pattern_key=pattern_key)


def aggregate_all(db: Database, settings: Settings, *, now: datetime, execute: bool = True) -> list[dict[str, Any]]:
    """원본이 있는 모든 노선을 다시 집계한다. 워커가 정적 갱신 뒤에 실행해 경유 순서 변경과 집계 창 이동을 함께 반영한다."""
    with db.transaction() as conn:
        if not repository.tables_ready(conn):
            return []
        route_ids = repository.routes_with_counts(conn)
    return [aggregate_route(db, settings, route_id, now=now, execute=execute) for route_id in route_ids]


def preview(db: Database, settings: Settings, parsed: ParsedFile, *, now: datetime) -> list[dict[str, Any]]:
    """저장하지 않고, 해석한 파일의 행만으로 집계 결과를 미리 본다(입력 전 확인용)."""
    if settings.congestion_window_months is None:
        raise CongestionConfigError("BACKEND_CONGESTION_WINDOW_MONTHS가 비어 있습니다.")
    window = (window_start(now, settings.congestion_window_months), now.astimezone(KST).date())
    by_route: dict[str, list[SourceRow]] = {}
    for row in parsed.rows:
        by_route.setdefault(row.route_label, []).append(row)
    out = []
    with db.transaction(isolation="REPEATABLE READ") as conn:
        for label, rows in sorted(by_route.items()):
            route, reason = resolve_route(conn, label)
            if route is None:
                out.append({"route_label": label, "status": "held", "reason": reason})
                continue
            patterns = repository.pattern_stops(conn, route["id"])
            stop_ids, _, _ = _map_stops(rows, patterns)
            counts = [CountRow(row.service_date, row.hour, row.stop_order, stop_ids.get(row.stop_order),
                               row.board + row.transfer, row.alight)
                      for row in rows if window[0] <= row.service_date <= window[1]]
            result, pattern_key = compute(route, patterns, counts, settings, conn=conn, window=window)
            out.append(_summary(route, result, window=window, saved=None, pattern_key=pattern_key))
    return out


# ---------------------------------------------------------------- 균형 시뮬레이션 입력

def equilibrium_options(conn: psycopg.Connection, search_id: UUID, policy: levels.LevelPolicy, *,
                        assume_trips_per_hour: float | None = None) -> list[equilibrium.Option]:
    """저장된 검색의 순위 후보를 균형 시뮬레이션 입력으로 바꾼다(읽기만 한다).

    같은 정적 경로를 차량만 바꿔 타는 조합은 순위가 가장 높은 하나만 쓴다(시간당 선택 인원을 그 경로의 차량들에 나눠
    싣기 때문). 버스 구간의 기준 재차와 통과 횟수는 승차 예상 시각의 집계 값이다. 집계가 없는 노선은 기준 재차를 0으로 두고
    통과 횟수를 assume_trips_per_hour(없으면 노선 배차간격)로 채우며, 그 사실을 assumed에 남긴다. 통과 횟수도 구하지 못하면
    그 구간은 혼잡 비용 없이 둔다.
    """
    rows, seen = [], set()
    for row in repository.ranked_candidates(conn, search_id):
        if row["static_route_key"] not in seen and row["time_seconds"] is not None:
            seen.add(row["static_route_key"])
            rows.append(row)
    bus_legs = [leg for row in rows for leg in row["legs"] if leg.get("kind") == "bus"]
    keys = {(leg.get("route_internal_id"), leg.get("pattern_key")) for leg in bus_legs}
    keys = {key for key in keys if key[0] is not None and key[1]}
    profiles = repository.profiles_for(conn, keys) if keys and repository.tables_ready(conn) else {}
    routes = {route_id: repository.route_by_id(conn, route_id) for route_id, _ in keys}

    def ride(leg: dict[str, Any]) -> equilibrium.Ride:
        key = (leg.get("route_internal_id"), leg.get("pattern_key"))
        seconds = float(leg.get("ride_seconds") or 0.0)
        at = repository.moment((leg.get("board_eta") or {}).get("predicted_arrival_at"))
        first, last = (leg.get("board") or {}).get("seq"), (leg.get("alight") or {}).get("seq")
        if at is None or first is None or last is None:
            return equilibrium.Ride(key, seconds, (), ("no_congestion_estimate",))
        seqs = range(int(first), max(int(first) + 1, int(last)))
        if profiles.get(key):
            links = []
            for seq in seqs:
                found = levels.describe(profiles[key], policy, at=at, from_seq=seq)
                if found["status"] == "available":
                    links.append(equilibrium.Link(seq, found["load_per_vehicle"], found["trips_per_hour"]))
            notes = () if len(links) == len(seqs) else ("some_links_without_estimate",)
            return equilibrium.Ride(key, seconds, tuple(links), notes if links else ("no_congestion_estimate",))
        trips, note = assume_trips_per_hour, "trips_assumed"
        if trips is None:
            kind = day_type(at.astimezone(KST).date(), policy.holidays)
            service_info = (routes.get(key[0]) or {}).get("service_info")
            trips, note = timetable_trips_per_hour(service_info, kind), "trips_from_timetable"
        if not trips:
            return equilibrium.Ride(key, seconds, (), ("no_congestion_estimate",))
        return equilibrium.Ride(key, seconds, tuple(equilibrium.Link(seq, 0.0, trips) for seq in seqs),
                                ("base_load_unknown", note))

    options = []
    for row in rows:
        buses = [leg for leg in row["legs"] if leg.get("kind") == "bus"]
        options.append(equilibrium.Option(
            option_id=str(row["id"]), time_seconds=row["time_seconds"], rides=tuple(ride(leg) for leg in buses),
            label=" → ".join(str(leg.get("route_no") or leg.get("route_id")) for leg in buses) or None))
    return options


# ---------------------------------------------------------------- 조회

class Lookup:
    """검색·도착 목록이 한 번 읽어 여러 구간에 쓰는 집계 묶음."""

    def __init__(self, policy: levels.LevelPolicy | None,
                 profiles: dict[tuple[int, str], dict[tuple[str, int, int], dict[str, Any]]] | None = None,
                 guided: dict[tuple[int, str], list[levels.GuidedRide]] | None = None) -> None:
        self.policy = policy
        self.profiles = profiles or {}
        # (노선 내부 ID, 패턴) → 앱이 안내 중인 탑승. 검색에서만 읽는다(정류소 도착 목록에는 더하지 않는다).
        self.guided = guided or {}

    @classmethod
    def load(cls, conn: psycopg.Connection, policy: levels.LevelPolicy | None,
             targets: Iterable[tuple[int | None, str | None]], *, guided_at: datetime | None = None,
             exclude_search_id: UUID | None = None) -> Lookup:
        """(노선 내부 ID, 패턴) 대상의 집계를 읽는다. 단계 정책이나 테이블이 없으면 읽지 않고 미확인으로 답한다.

        guided_at을 주면 그 시각에 아직 끝나지 않은 안내 중 탑승도 읽어 1대당 재차에 더한다. exclude_search_id는 그
        검색을 요청한 사용자 자신의 이동을 빼는 데 쓴다.
        """
        wanted = [(route_id, key) for route_id, key in targets if route_id is not None and key]
        if policy is None or not wanted or not repository.tables_ready(conn):
            return cls(policy)
        profiles = repository.profiles_for(conn, wanted)
        guided: dict[tuple[int, str], list[levels.GuidedRide]] = {}
        if guided_at is not None and profiles:
            guided = {key: [levels.GuidedRide(**ride) for ride in rides]
                      for key, rides in repository.guided_rides(conn, list(profiles), now=guided_at,
                                                                exclude_search_id=exclude_search_id).items()}
        return cls(policy, profiles, guided)

    def describe(self, route_id: int | None, pattern_key: str | None, *, at: datetime | None, from_seq: int | None,
                 to_seq: int | None = None, vehicle_id: str | None = None) -> dict[str, Any]:
        if route_id is None or not pattern_key or from_seq is None:
            return levels.unavailable(levels.PROFILE_MISSING, self.policy)
        return levels.describe(self.profiles.get((route_id, pattern_key)), self.policy, at=at, from_seq=from_seq,
                               to_seq=to_seq, guided=self.guided.get((route_id, pattern_key), ()),
                               vehicle_id=vehicle_id)
