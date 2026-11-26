"""혼잡도 테이블(app.ridership_counts·app.congestion_profiles)의 조회·저장. 적용 DDL: migrations/13_congestion.sql.

연결·트랜잭션은 호출자가 관리한다. 원본은 추가만 하고(같은 노선·일자·시간·순번은 건너뜀), 집계는 노선 단위로 교체한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg

from app.modules.congestion.aggregation import CountRow, ProfileRow

PROVIDER = "tago"
_PROFILE_KEYS = ("flow_mean", "flow_p80", "trips_per_hour", "trips_source", "sample_days", "source_period")


def tables_ready(conn: psycopg.Connection) -> bool:
    """혼잡도 테이블이 적용돼 있는지. 적용 전 DB에서는 혼잡 정보를 미확인으로 두고 다른 기능을 막지 않는다."""
    row = conn.execute("SELECT to_regclass('app.ridership_counts') IS NOT NULL "
                       "AND to_regclass('app.congestion_profiles') IS NOT NULL").fetchone()
    return bool(row[0])


def routes_by_number(conn: psycopg.Connection, route_no: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, external_id, route_no, start_name, end_name, service_info FROM app.routes "
        "WHERE provider = %s AND is_active AND route_no = %s ORDER BY id", (PROVIDER, route_no)).fetchall()
    keys = ("id", "external_id", "route_no", "start_name", "end_name", "service_info")
    return [dict(zip(keys, row)) for row in rows]


def route_by_id(conn: psycopg.Connection, route_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, external_id, route_no, start_name, end_name, service_info FROM app.routes "
                       "WHERE id = %s", (route_id,)).fetchone()
    keys = ("id", "external_id", "route_no", "start_name", "end_name", "service_info")
    return None if row is None else dict(zip(keys, row))


def pattern_stops(conn: psycopg.Connection, route_id: int) -> dict[str, list[dict[str, Any]]]:
    """노선의 현재 패턴별 경유 정류소(순번 순서): seq, stop_id, stop_no, name."""
    result: dict[str, list[dict[str, Any]]] = {}
    for key, seq, stop_id, stop_no, name in conn.execute(
            "SELECT rs.pattern_key, rs.seq, st.id, st.stop_no, st.name FROM app.route_stops rs "
            "JOIN app.stops st ON st.id = rs.stop_id WHERE rs.route_id = %s ORDER BY rs.pattern_key, rs.seq",
            (route_id,)):
        result.setdefault(key, []).append({"seq": seq, "stop_id": stop_id, "stop_no": stop_no, "name": name})
    return result


def insert_counts(conn: psycopg.Connection, rows: Sequence[tuple], *, source_file: str,
                  uploaded_by: UUID | None) -> tuple[int, int, int]:
    """원본 행을 추가한다. (새로 넣은 행, 이미 있어 건너뛴 행, 건너뛴 행 중 값이 다른 행).

    rows: (route_id, service_date, hour, source_stop_order, source_stop_id, source_stop_name, stop_id,
           board_count, alight_count, transfer_board_count). 이미 있는 키의 값은 바꾸지 않는다.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS ridership_incoming (route_id BIGINT, service_date DATE, hour SMALLINT, "
                 "source_stop_order INTEGER, source_stop_id TEXT, source_stop_name TEXT, stop_id BIGINT, "
                 "board_count INTEGER, alight_count INTEGER, transfer_board_count INTEGER) ON COMMIT DROP")
    conn.execute("TRUNCATE ridership_incoming")
    with conn.cursor().copy("COPY ridership_incoming FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    existing, differing = conn.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE (r.board_count, r.alight_count, r.transfer_board_count)
                                                IS DISTINCT FROM (i.board_count, i.alight_count, i.transfer_board_count))
          FROM ridership_incoming i JOIN app.ridership_counts r
            ON (r.route_id, r.service_date, r.hour, r.source_stop_order)
             = (i.route_id, i.service_date, i.hour, i.source_stop_order)
        """).fetchone()
    inserted = conn.execute(
        """
        INSERT INTO app.ridership_counts (route_id, service_date, hour, source_stop_order, source_stop_id,
            source_stop_name, stop_id, board_count, alight_count, transfer_board_count, source_file, uploaded_by)
        SELECT route_id, service_date, hour, source_stop_order, source_stop_id, source_stop_name, stop_id,
               board_count, alight_count, transfer_board_count, %s, %s
          FROM ridership_incoming
        ON CONFLICT ON CONSTRAINT ridership_counts_key DO NOTHING
        """, (source_file, uploaded_by)).rowcount
    return inserted, existing, differing


def existing_keys(conn: psycopg.Connection, route_id: int, first: date, last: date) -> dict[tuple, tuple]:
    """기간 안에 이미 있는 원본 행: (일자, 시간, 순번) → (단순승차, 하차, 환승승차). 저장 없이 입력 결과를 미리 볼 때 쓴다."""
    rows = conn.execute(
        "SELECT service_date, hour, source_stop_order, board_count, alight_count, transfer_board_count "
        "FROM app.ridership_counts WHERE route_id = %s AND service_date BETWEEN %s AND %s",
        (route_id, first, last)).fetchall()
    return {(day, hour, order): (board, alight, transfer) for day, hour, order, board, alight, transfer in rows}


def counts_in_window(conn: psycopg.Connection, route_id: int, first: date, last: date) -> list[CountRow]:
    rows = conn.execute(
        "SELECT service_date, hour, source_stop_order, stop_id, total_board_count, alight_count "
        "FROM app.ridership_counts WHERE route_id = %s AND service_date BETWEEN %s AND %s", (route_id, first, last))
    return [CountRow(*row) for row in rows]


def position_history(conn: psycopg.Connection, route_id: int, pattern_key: str, target_key: str, first: datetime,
                     last: datetime) -> tuple[list[datetime], list[tuple[datetime, str, int]]]:
    """노선 위치의 공개 수집 이력: (수집 시각 목록, [(수집 시각, 차량번호, 경유 순번)]).

    수집 시각 목록에는 차량이 한 대도 없던 수집도 들어간다(관측이 덮은 시간을 세는 기준). target_key는 위치 수집 대상 키다.
    """
    snapshots = [row[0] for row in conn.execute(
        "SELECT min(received_at) FROM app.api_fetches WHERE api_kind = 'positions' AND target_key = %s "
        "AND published_at IS NOT NULL AND received_at >= %s AND received_at < %s GROUP BY batch_key ORDER BY 1",
        (target_key, first, last))]
    points = conn.execute(
        "SELECT collected_at, vehicle_no, stop_seq FROM app.transit_observations WHERE kind = 'position' "
        "AND route_id = %s AND pattern_key = %s AND vehicle_no IS NOT NULL AND stop_seq IS NOT NULL "
        "AND collected_at >= %s AND collected_at < %s ORDER BY vehicle_no, collected_at",
        (route_id, pattern_key, first, last)).fetchall()
    return snapshots, points


def replace_profiles(conn: psycopg.Connection, route_id: int, profiles: Iterable[ProfileRow]) -> int:
    """노선의 집계 행을 새 결과로 교체한다(호출자의 한 트랜잭션 안에서). 넣은 행 수."""
    conn.execute("DELETE FROM app.congestion_profiles WHERE route_id = %s", (route_id,))
    count = 0
    with conn.cursor().copy(
            "COPY app.congestion_profiles (route_id, pattern_key, stop_seq, day_type, hour, flow_mean, flow_p80, "
            "trips_per_hour, trips_source, sample_days, source_period) FROM STDIN") as copy:
        for row in profiles:
            copy.write_row((route_id, row.pattern_key, row.stop_seq, row.day_type, row.hour, row.flow_mean,
                            row.flow_p80, row.trips_per_hour, row.trips_source, row.sample_days, row.source_period))
            count += 1
    return count


def routes_with_counts(conn: psycopg.Connection) -> list[int]:
    """승하차 원본이 있는 노선. 워커가 자주 부르므로 고유 키 색인을 노선 단위로 건너뛰며 읽는다(행 전체를 훑지 않음)."""
    rows = conn.execute(
        """
        WITH RECURSIVE found AS (
            (SELECT route_id FROM app.ridership_counts ORDER BY route_id LIMIT 1)
            UNION ALL
            SELECT (SELECT c.route_id FROM app.ridership_counts c WHERE c.route_id > found.route_id
                     ORDER BY c.route_id LIMIT 1)
              FROM found WHERE found.route_id IS NOT NULL
        )
        SELECT route_id FROM found WHERE route_id IS NOT NULL ORDER BY 1
        """).fetchall()
    return [row[0] for row in rows]


def route_external_ids(conn: psycopg.Connection, route_ids: Iterable[int]) -> list[str]:
    ids = sorted(set(route_ids))
    if not ids:
        return []
    return [row[0] for row in conn.execute(
        "SELECT external_id FROM app.routes WHERE id = ANY(%s) AND is_active ORDER BY 1", (ids,))]


def profiles_for(conn: psycopg.Connection, targets: Iterable[tuple[int, str]]
                 ) -> dict[tuple[int, str], dict[tuple[str, int, int], dict[str, Any]]]:
    """(노선 내부 ID, 패턴) → {(요일유형, 시간대, 경유 순번) → 집계 행}. 현재 패턴과 키가 다른 예전 집계는 조회되지 않는다."""
    wanted = sorted(set(targets))
    result: dict[tuple[int, str], dict[tuple[str, int, int], dict[str, Any]]] = {}
    if not wanted:
        return result
    rows = conn.execute(
        "SELECT route_id, pattern_key, day_type, hour, stop_seq, flow_mean, flow_p80, trips_per_hour, trips_source, "
        "sample_days, source_period FROM app.congestion_profiles "
        "WHERE route_id = ANY(%s) AND pattern_key = ANY(%s)",
        ([route_id for route_id, _ in wanted], [key for _, key in wanted]))
    allowed = set(wanted)
    for route_id, key, kind, hour, seq, *values in rows:
        if (route_id, key) in allowed:
            result.setdefault((route_id, key), {})[(kind, hour, seq)] = dict(zip(_PROFILE_KEYS, values))
    return result


def guided_rides(conn: psycopg.Connection, targets: Iterable[tuple[int, str]], *, now: datetime,
                 exclude_search_id: UUID | None = None) -> dict[tuple[int, str], list[dict[str, Any]]]:
    """(노선 내부 ID, 패턴) → 앱이 안내 중인 탑승. 활성 이동의 선택 경로에서 아직 끝나지 않은 버스 구간이다.

    계획한 하차 시각(없으면 승차 시각)이 now보다 앞이면 이미 끝난 탑승으로 보고 뺀다(종료하지 않고 방치된 이동 포함).
    exclude_search_id를 주면 그 검색을 요청한 사용자의 이동은 세지 않는다(자기 자신의 탑승을 혼잡으로 더하지 않는다).
    """
    wanted = set(targets)
    result: dict[tuple[int, str], list[dict[str, Any]]] = {}
    if not wanted:
        return result
    rows = conn.execute(
        """
        SELECT leg, j.current_progress -> 'completed_bus_legs'
          FROM app.journeys j, jsonb_array_elements(j.selected_snapshot -> 'legs') AS leg
         WHERE j.lifecycle_status = 'active' AND leg ->> 'kind' = 'bus'
           AND (%(search)s::uuid IS NULL OR j.user_id IS DISTINCT FROM
                (SELECT s.user_id FROM app.route_searches s WHERE s.id = %(search)s::uuid))
        """, {"search": exclude_search_id})
    for leg, completed in rows:
        key = (leg.get("route_internal_id"), leg.get("pattern_key"))
        board_seq, alight_seq = (leg.get("board") or {}).get("seq"), (leg.get("alight") or {}).get("seq")
        if key not in wanted or board_seq is None or alight_seq is None or leg.get("leg_index") in (completed or []):
            continue
        board_at = moment((leg.get("board_eta") or {}).get("predicted_arrival_at"))
        ends_at = moment((leg.get("alight_eta") or {}).get("predicted_arrival_at")) or board_at
        if ends_at is None or ends_at < now:
            continue
        result.setdefault(key, []).append({
            "from_seq": int(board_seq), "to_seq": int(alight_seq), "board_at": board_at,
            "vehicle_id": (leg.get("vehicle") or {}).get("vehicle_id")})
    return result


def ranked_candidates(conn: psycopg.Connection, search_id: UUID) -> list[dict[str, Any]]:
    """검색의 순위 후보(순위 순): 정적 경로 키, 순위, 구간, 순위 계산에 쓴 시간(초)."""
    rows = conn.execute(
        "SELECT id, static_route_key, rank_no, legs, evaluation -> 'ranking' ->> 'time_seconds' "
        "FROM app.route_candidates WHERE search_id = %s AND rank_no IS NOT NULL ORDER BY rank_no", (search_id,))
    return [{"id": row[0], "static_route_key": row[1], "rank_no": row[2], "legs": row[3] or [],
             "time_seconds": None if row[4] is None else float(row[4])} for row in rows]


def moment(value: Any) -> datetime | None:
    """ISO 8601 글자를 시간대가 있는 시각으로. 읽지 못하거나 시간대가 없으면 None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """노선별 원본 입력 현황과 집계 현황."""
    rows = conn.execute(
        """
        SELECT r.id, r.route_no, r.external_id, c.rows, c.days, c.first_day, c.last_day, c.hours, c.files,
               p.rows, p.patterns, p.computed_at, p.source_period,
               (SELECT array_agg(DISTINCT rs.pattern_key) FROM app.route_stops rs WHERE rs.route_id = r.id)
          FROM app.routes r
          JOIN (SELECT route_id, count(*) AS rows, count(DISTINCT service_date) AS days, min(service_date) AS first_day,
                       max(service_date) AS last_day, array_agg(DISTINCT hour ORDER BY hour) AS hours,
                       count(DISTINCT source_file) AS files
                  FROM app.ridership_counts GROUP BY route_id) c ON c.route_id = r.id
          LEFT JOIN (SELECT route_id, count(*) AS rows, array_agg(DISTINCT pattern_key) AS patterns,
                            max(computed_at) AS computed_at, max(source_period) AS source_period
                       FROM app.congestion_profiles GROUP BY route_id) p ON p.route_id = r.id
         ORDER BY r.route_no, r.id
        """).fetchall()
    keys = ("route_id", "route_no", "external_id", "count_rows", "days", "first_day", "last_day", "hours", "files",
            "profile_rows", "profile_patterns", "computed_at", "source_period", "current_patterns")
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["profiles_match_current_pattern"] = bool(item["profile_patterns"]) and \
            set(item["profile_patterns"]) <= set(item["current_patterns"] or [])
        out.append(item)
    return out
