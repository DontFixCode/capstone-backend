"""교통정보 테이블(app.stops·routes·route_stops·api_fetches·transit_observations)의 조회·저장.

연결·트랜잭션은 호출자가 app/db로 관리한다. PostGIS는 적용 DDL과 같이 extensions 스키마의 함수를 명시해 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.modules.transit.models import FetchAttemptRow, ObservationRow, RouteRow, RouteStopRow, StopRow

PROVIDER = "tago"


def _provider(target_key: str) -> str:
    """수집 키의 제공처(접두사). 도착정보는 대전시(daejeon), 위치·정적 정보는 TAGO(tago)다."""
    return target_key.split(":", 1)[0]
_POINT = "extensions.ST_SetSRID(extensions.ST_MakePoint({lon}, {lat}), 4326)::extensions.geography"


def clock_now(conn: psycopg.Connection) -> datetime:
    """트랜잭션 시작 시각이 아닌 실제 현재 시각."""
    return conn.execute("SELECT clock_timestamp()").fetchone()[0]


# ---------------------------------------------------------------- api_fetches

def insert_fetch(conn: psycopg.Connection, row: FetchAttemptRow) -> int:
    return conn.execute(
        """
        INSERT INTO app.api_fetches (batch_key, provider, api_kind, target_key, request_params, page_no, attempt_no,
            requested_at, received_at, http_status, provider_code, result_status, total_count, raw_body,
            normalizer_version, error_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (row.batch_key, row.provider, row.api_kind, row.target_key, Jsonb(row.request_params), row.page_no,
         row.attempt_no, row.requested_at, row.received_at, row.http_status, row.provider_code, row.result_status,
         row.total_count, row.raw_body, row.normalizer_version, row.error_reason),
    ).fetchone()[0]


def mark_unpublished(conn: psycopg.Connection, ids: Sequence[int], reason: str, *, as_partial: bool) -> None:
    """공개하지 않는 배치의 행에 사유를 남긴다. as_partial이면 정상 수신 행을 partial로 표시한다."""
    if not ids:
        return
    conn.execute(
        """
        UPDATE app.api_fetches
           SET error_reason = CASE WHEN error_reason IS NULL THEN %(reason)s ELSE error_reason || ' | ' || %(reason)s END,
               result_status = CASE WHEN %(partial)s AND result_status IN ('success', 'empty') THEN 'partial'
                                    ELSE result_status END
         WHERE id = ANY(%(ids)s) AND published_at IS NULL
        """,
        {"reason": reason, "partial": as_partial, "ids": list(ids)},
    )


def mark_published(conn: psycopg.Connection, ids: Sequence[int], published_at: datetime) -> int:
    cur = conn.execute(
        "UPDATE app.api_fetches SET published_at = %s WHERE id = ANY(%s) AND published_at IS NULL "
        "AND result_status IN ('success', 'empty')",
        (published_at, list(ids)),
    )
    return cur.rowcount


def latest_published_pages(conn: psycopg.Connection, api_kind: str, target_key: str) -> list[dict[str, Any]]:
    """대상의 마지막 공개 배치에 속한 채택 행 전체(페이지 순). 다른 배치의 페이지와 섞지 않는다."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT batch_key FROM app.api_fetches
             WHERE provider = %(p)s AND api_kind = %(k)s AND target_key = %(t)s AND published_at IS NOT NULL
             ORDER BY published_at DESC, id DESC
             LIMIT 1)
        SELECT f.id, f.batch_key, f.page_no, f.result_status, f.requested_at, f.received_at, f.published_at,
               f.total_count
          FROM app.api_fetches f JOIN latest l ON l.batch_key = f.batch_key
         WHERE f.published_at IS NOT NULL
         ORDER BY f.page_no
        """,
        {"p": _provider(target_key), "k": api_kind, "t": target_key},
    ).fetchall()
    keys = ("id", "batch_key", "page_no", "result_status", "requested_at", "received_at", "published_at", "total_count")
    return [dict(zip(keys, row)) for row in rows]


def last_attempt(conn: psycopg.Connection, api_kind: str, target_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, batch_key, page_no, attempt_no, result_status, requested_at, published_at, error_reason
          FROM app.api_fetches
         WHERE provider = %s AND api_kind = %s AND target_key = %s
         ORDER BY requested_at DESC, id DESC
         LIMIT 1
        """,
        (_provider(target_key), api_kind, target_key),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "batch_key", "page_no", "attempt_no", "result_status", "requested_at", "published_at", "error_reason")
    return dict(zip(keys, row))


def batch_is_published(conn: psycopg.Connection, batch_key: UUID) -> bool:
    return bool(conn.execute(
        "SELECT EXISTS (SELECT 1 FROM app.api_fetches WHERE batch_key = %s AND published_at IS NOT NULL)", (batch_key,)
    ).fetchone()[0])


def target_timings(
    conn: psycopg.Connection, api_kind: str, target_keys: Sequence[str],
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """대상별 (마지막 시도 시각, 마지막 공개 배치의 가장 이른 수신 시각)."""
    if not target_keys:
        return {}
    result: dict[str, tuple[datetime | None, datetime | None]] = {key: (None, None) for key in target_keys}
    providers = sorted({_provider(key) for key in target_keys})
    for key, last in conn.execute(
        "SELECT target_key, max(requested_at) FROM app.api_fetches "
        "WHERE provider = ANY(%s) AND api_kind = %s AND target_key = ANY(%s) GROUP BY target_key",
        (providers, api_kind, list(target_keys)),
    ):
        result[key] = (last, None)
    for key, collected in conn.execute(
        """
        WITH lb AS (
            SELECT DISTINCT ON (target_key) target_key, batch_key FROM app.api_fetches
             WHERE provider = ANY(%s) AND api_kind = %s AND target_key = ANY(%s) AND published_at IS NOT NULL
             ORDER BY target_key, published_at DESC, id DESC)
        SELECT lb.target_key, min(f.received_at)
          FROM lb JOIN app.api_fetches f ON f.batch_key = lb.batch_key AND f.published_at IS NOT NULL
         GROUP BY lb.target_key
        """,
        (providers, api_kind, list(target_keys)),
    ):
        result[key] = (result[key][0], collected)
    return result


def usage(conn: psycopg.Connection, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """기간 안의 실제 HTTP 시도 수(최초·페이지·실패·재시도 포함)."""
    rows = conn.execute(
        """
        SELECT api_kind,
               count(*) AS http_attempts,
               count(*) FILTER (WHERE attempt_no = 1) AS first_attempts,
               count(*) FILTER (WHERE attempt_no > 1) AS retries,
               count(*) FILTER (WHERE page_no > 1) AS later_pages,
               count(*) FILTER (WHERE result_status IN ('error', 'timeout')) AS failed,
               count(*) FILTER (WHERE result_status = 'partial') AS partial,
               count(*) FILTER (WHERE published_at IS NOT NULL) AS published,
               count(DISTINCT batch_key) AS batches
          FROM app.api_fetches
         WHERE provider = %s AND requested_at >= %s AND requested_at < %s
         GROUP BY api_kind
         ORDER BY api_kind
        """,
        (PROVIDER, since, until),
    ).fetchall()
    keys = ("api_kind", "http_attempts", "first_attempts", "retries", "later_pages", "failed", "partial", "published",
            "batches")
    return [dict(zip(keys, row)) for row in rows]


# ---------------------------------------------------------------- 정적 공개본

def current_static_versions(conn: psycopg.Connection) -> list[UUID]:
    """활성 routes의 data_version 목록. 정상이면 1개이며 2개 이상이면 혼합 공개다."""
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT data_version FROM app.routes WHERE provider = %s AND is_active ORDER BY 1", (PROVIDER,)
    )]


def static_fetch_status(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT max(requested_at), max(published_at)
          FROM app.api_fetches WHERE provider = %s AND api_kind IN ('stops', 'routes', 'route_stops')
        """,
        (PROVIDER,),
    ).fetchone()
    reasons = conn.execute(
        """
        SELECT target_key, result_status, error_reason FROM app.api_fetches
         WHERE provider = %s AND api_kind IN ('stops', 'routes', 'route_stops') AND error_reason IS NOT NULL
           AND requested_at > coalesce((SELECT max(published_at) FROM app.api_fetches
                                         WHERE provider = %s AND api_kind IN ('stops', 'routes', 'route_stops')),
                                       '-infinity')
         ORDER BY requested_at DESC, id DESC LIMIT 20
        """,
        (PROVIDER, PROVIDER),
    ).fetchall()
    return {
        "last_attempt_at": row[0],
        "last_published_at": row[1],
        "reasons_since_last_publish": [
            {"target_key": t, "result_status": s, "error_reason": r} for t, s, r in reasons
        ],
    }


def static_counts(conn: psycopg.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM app.stops WHERE provider = %(p)s AND is_active),
               (SELECT count(*) FROM app.stops WHERE provider = %(p)s AND is_active AND location IS NULL),
               (SELECT count(*) FROM app.stops WHERE provider = %(p)s AND NOT is_active),
               (SELECT count(*) FROM app.routes WHERE provider = %(p)s AND is_active),
               (SELECT count(*) FROM app.routes WHERE provider = %(p)s AND NOT is_active),
               (SELECT count(*) FROM app.route_stops rs JOIN app.routes r ON r.id = rs.route_id
                 WHERE r.provider = %(p)s AND r.is_active)
        """,
        {"p": PROVIDER},
    ).fetchone()
    keys = ("active_stops", "active_stops_without_location", "inactive_stops", "active_routes", "inactive_routes",
            "route_stops")
    return dict(zip(keys, row))


def publish_static(
    conn: psycopg.Connection, version: UUID, ts: datetime,
    stops: Sequence[StopRow], routes: Sequence[RouteRow], route_stops: Sequence[RouteStopRow],
) -> dict[str, int]:
    """한 트랜잭션에서 정적 3개 테이블을 같은 data_version으로 교체한다. 커밋은 호출자가 한다.

    정류소·노선은 (provider, external_id)로 기존 내부 ID를 유지하고, 이번 원천에 없는 활성 행은 비활성화한다.
    경유 순서는 기존 패턴 전체를 지우고 다시 넣는다(다른 테이블이 경유 항목 ID를 참조하지 않는다).
    """
    conn.execute(
        "CREATE TEMP TABLE stage_stops (external_id text PRIMARY KEY, name text, stop_no text, "
        "lat double precision, lon double precision, source_fetch_id bigint) ON COMMIT DROP"
    )
    conn.execute(
        "CREATE TEMP TABLE stage_routes (external_id text PRIMARY KEY, route_no text, route_type text, "
        "start_name text, end_name text, service_info jsonb, source_fetch_id bigint) ON COMMIT DROP"
    )
    conn.execute(
        "CREATE TEMP TABLE stage_route_stops (route_external_id text, pattern_key text, direction_code text, "
        "seq integer, source_node_order text, stop_external_id text, source_fetch_id bigint, "
        "PRIMARY KEY (route_external_id, pattern_key, seq)) ON COMMIT DROP"
    )
    with conn.cursor() as cur:
        with cur.copy("COPY stage_stops FROM STDIN") as copy:
            for s in stops:
                copy.write_row((s.external_id, s.name, s.stop_no, s.lat, s.lon, s.source_fetch_id))
        with cur.copy("COPY stage_routes FROM STDIN") as copy:
            for r in routes:
                copy.write_row((r.external_id, r.route_no, r.route_type, r.start_name, r.end_name,
                                Jsonb(r.service_info), r.source_fetch_id))
        with cur.copy("COPY stage_route_stops FROM STDIN") as copy:
            for rs in route_stops:
                copy.write_row((rs.route_external_id, rs.pattern_key, rs.direction_code, rs.seq,
                                rs.source_node_order, rs.stop_external_id, rs.source_fetch_id))
    params = {"p": PROVIDER, "v": version, "ts": ts}
    counts: dict[str, int] = {}
    counts["stops_upserted"] = conn.execute(
        f"""
        INSERT INTO app.stops (provider, external_id, name, stop_no, location, data_version, source_fetch_id,
                               is_active, updated_at)
        SELECT %(p)s, s.external_id, s.name, s.stop_no,
               CASE WHEN s.lat IS NULL OR s.lon IS NULL THEN NULL ELSE {_POINT.format(lon='s.lon', lat='s.lat')} END,
               %(v)s, s.source_fetch_id, TRUE, %(ts)s
          FROM stage_stops s
        ON CONFLICT (provider, external_id) DO UPDATE
           SET name = EXCLUDED.name, stop_no = EXCLUDED.stop_no, location = EXCLUDED.location,
               data_version = EXCLUDED.data_version, source_fetch_id = EXCLUDED.source_fetch_id,
               is_active = TRUE, updated_at = EXCLUDED.updated_at
        """,
        params,
    ).rowcount
    counts["stops_deactivated"] = conn.execute(
        "UPDATE app.stops t SET is_active = FALSE, updated_at = %(ts)s WHERE t.provider = %(p)s AND t.is_active "
        "AND NOT EXISTS (SELECT 1 FROM stage_stops s WHERE s.external_id = t.external_id)",
        params,
    ).rowcount
    counts["routes_upserted"] = conn.execute(
        """
        INSERT INTO app.routes (provider, external_id, route_no, route_type, start_name, end_name, service_info,
                                data_version, source_fetch_id, is_active, updated_at)
        SELECT %(p)s, r.external_id, r.route_no, r.route_type, r.start_name, r.end_name, r.service_info,
               %(v)s, r.source_fetch_id, TRUE, %(ts)s
          FROM stage_routes r
        ON CONFLICT (provider, external_id) DO UPDATE
           SET route_no = EXCLUDED.route_no, route_type = EXCLUDED.route_type, start_name = EXCLUDED.start_name,
               end_name = EXCLUDED.end_name, service_info = EXCLUDED.service_info,
               data_version = EXCLUDED.data_version, source_fetch_id = EXCLUDED.source_fetch_id,
               is_active = TRUE, updated_at = EXCLUDED.updated_at
        """,
        params,
    ).rowcount
    counts["routes_deactivated"] = conn.execute(
        "UPDATE app.routes t SET is_active = FALSE, updated_at = %(ts)s WHERE t.provider = %(p)s AND t.is_active "
        "AND NOT EXISTS (SELECT 1 FROM stage_routes s WHERE s.external_id = t.external_id)",
        params,
    ).rowcount
    counts["route_stops_deleted"] = conn.execute(
        "DELETE FROM app.route_stops rs USING app.routes r WHERE rs.route_id = r.id AND r.provider = %(p)s", params
    ).rowcount
    counts["route_stops_inserted"] = conn.execute(
        """
        INSERT INTO app.route_stops (route_id, pattern_key, direction_code, seq, source_node_order, stop_id,
                                     cumulative_distance_m, data_version, source_fetch_id)
        SELECT r.id, s.pattern_key, s.direction_code, s.seq, s.source_node_order, st.id, NULL, %(v)s,
               s.source_fetch_id
          FROM stage_route_stops s
          JOIN app.routes r ON r.provider = %(p)s AND r.external_id = s.route_external_id
          JOIN app.stops st ON st.provider = %(p)s AND st.external_id = s.stop_external_id
        """,
        params,
    ).rowcount
    return counts


def published_static_rows(conn: psycopg.Connection) -> dict[str, Any]:
    """현재 활성 정적 행 전체(대조용). 좌표는 소수 7자리로 반올림해 비교 오차를 없앤다."""
    stops = {
        ext: (name, no, None if lat is None else round(lat, 7), None if lon is None else round(lon, 7), str(v))
        for ext, name, no, lat, lon, v in conn.execute(
            "SELECT external_id, name, stop_no, extensions.ST_Y(location::extensions.geometry), "
            "extensions.ST_X(location::extensions.geometry), data_version FROM app.stops "
            "WHERE provider = %s AND is_active",
            (PROVIDER,),
        )
    }
    routes = {
        ext: (no, tp, start, end, str(v))
        for ext, no, tp, start, end, v in conn.execute(
            "SELECT external_id, route_no, route_type, start_name, end_name, data_version FROM app.routes "
            "WHERE provider = %s AND is_active",
            (PROVIDER,),
        )
    }
    sequences: dict[str, list[tuple[Any, ...]]] = {}
    versions: set[str] = set()
    for route_ext, pattern, direction, seq, order, stop_ext, v in conn.execute(
        """
        SELECT r.external_id, rs.pattern_key, rs.direction_code, rs.seq, rs.source_node_order, st.external_id,
               rs.data_version
          FROM app.route_stops rs
          JOIN app.routes r ON r.id = rs.route_id
          JOIN app.stops st ON st.id = rs.stop_id
         WHERE r.provider = %s AND r.is_active
         ORDER BY r.external_id, rs.pattern_key, rs.seq
        """,
        (PROVIDER,),
    ):
        sequences.setdefault(route_ext, []).append((pattern, direction, seq, order, stop_ext))
        versions.add(str(v))
    return {"stops": stops, "routes": routes, "sequences": sequences, "route_stop_versions": versions}


def read_static_network(
    conn: psycopg.Connection,
) -> tuple[list[UUID], list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """현재 활성 정적 공개본(버전, 정류소, 경유 순서, 노선). 호출자는 REPEATABLE READ 트랜잭션 안에서 부른다."""
    versions = current_static_versions(conn)
    routes = conn.execute(
        "SELECT id, external_id, route_no, route_type FROM app.routes WHERE provider = %s AND is_active "
        "ORDER BY external_id",
        (PROVIDER,),
    ).fetchall()
    stops = conn.execute(
        "SELECT id, external_id, name, extensions.ST_Y(location::extensions.geometry), "
        "extensions.ST_X(location::extensions.geometry) FROM app.stops WHERE provider = %s AND is_active "
        "ORDER BY external_id",
        (PROVIDER,),
    ).fetchall()
    patterns = conn.execute(
        """
        SELECT r.external_id, rs.pattern_key, rs.seq, st.external_id, rs.direction_code
          FROM app.route_stops rs
          JOIN app.routes r ON r.id = rs.route_id
          JOIN app.stops st ON st.id = rs.stop_id
         WHERE r.provider = %s AND r.is_active
         ORDER BY r.external_id, rs.pattern_key, rs.seq
        """,
        (PROVIDER,),
    ).fetchall()
    return versions, stops, patterns, routes


def nearby_stops(
    conn: psycopg.Connection, lat: float, lon: float, radius_m: float, limit: int,
    *, served_version: UUID | None = None,
) -> list[tuple[Any, ...]]:
    """저장 좌표가 있는 활성 정류소 중 측지 거리 radius_m 이내(GiST + ST_DWithin).

    served_version을 주면 그 정적 공개본(route_stops.data_version)의 활성 노선이 실제로 경유하는 정류소만 남긴다
    (정류소 목록에 없는 시외 경유 정류소도 활성 노선의 경유 목록에 있으면 포함).
    """
    point = _POINT.format(lon="%(lon)s::double precision", lat="%(lat)s::double precision")
    served = ("" if served_version is None else
              """ AND EXISTS (SELECT 1 FROM app.route_stops rs JOIN app.routes r ON r.id = rs.route_id
                              WHERE rs.stop_id = s.id AND r.provider = %(p)s AND r.is_active
                                AND rs.data_version = %(v)s)""")
    return conn.execute(
        f"""
        SELECT s.id, s.external_id, s.name, s.stop_no, extensions.ST_Distance(s.location, q.g) AS distance_m,
               extensions.ST_Y(s.location::extensions.geometry), extensions.ST_X(s.location::extensions.geometry)
          FROM app.stops s, (SELECT {point} AS g) q
         WHERE s.provider = %(p)s AND s.is_active AND s.location IS NOT NULL
           AND extensions.ST_DWithin(s.location, q.g, %(r)s){served}
         ORDER BY distance_m, s.external_id
         LIMIT %(n)s
        """,
        {"p": PROVIDER, "lat": lat, "lon": lon, "r": radius_m, "n": limit, "v": served_version},
    ).fetchall()


def static_version_published(conn: psycopg.Connection, version: UUID) -> bool:
    """그 정적 공개본의 활성 노선 경유 행이 아직 있는지(검색 도중 새 공개본으로 교체됐는지 확인)."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM app.route_stops rs JOIN app.routes r ON r.id = rs.route_id "
        "WHERE r.provider = %s AND r.is_active AND rs.data_version = %s)", (PROVIDER, version)).fetchone()[0]


# ---------------------------------------------------------------- 실시간 정규화용 정적 조회

def route_ids(conn: psycopg.Connection, external_ids: Iterable[str]) -> dict[str, int]:
    ids = sorted(set(external_ids))
    if not ids:
        return {}
    return dict(conn.execute(
        "SELECT external_id, id FROM app.routes WHERE provider = %s AND is_active AND external_id = ANY(%s)",
        (PROVIDER, ids),
    ).fetchall())


def is_active_stop(conn: psycopg.Connection, external_id: str) -> bool:
    """현재 활성 정류소인지."""
    return conn.execute("SELECT 1 FROM app.stops WHERE provider = %s AND is_active AND external_id = %s",
                        (PROVIDER, external_id)).fetchone() is not None


def stop_ids(conn: psycopg.Connection, external_ids: Iterable[str]) -> dict[str, int]:
    ids = sorted(set(external_ids))
    if not ids:
        return {}
    return dict(conn.execute(
        "SELECT external_id, id FROM app.stops WHERE provider = %s AND is_active AND external_id = ANY(%s)",
        (PROVIDER, ids),
    ).fetchall())


def route_visits(conn: psycopg.Connection, internal_route_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = sorted(set(internal_route_ids))
    result: dict[int, list[dict[str, Any]]] = {route_id: [] for route_id in ids}
    if not ids:
        return result
    for route_id, pattern, seq, order, stop_ext, stop_id in conn.execute(
        """
        SELECT rs.route_id, rs.pattern_key, rs.seq, rs.source_node_order, st.external_id, st.id
          FROM app.route_stops rs JOIN app.stops st ON st.id = rs.stop_id
         WHERE rs.route_id = ANY(%s)
         ORDER BY rs.route_id, rs.pattern_key, rs.seq
        """,
        (ids,),
    ):
        result[route_id].append({"pattern_key": pattern, "seq": seq, "source_node_order": order,
                                 "stop_external_id": stop_ext, "stop_id": stop_id})
    return result


def insert_observations(conn: psycopg.Connection, rows: Sequence[ObservationRow]) -> int:
    if not rows:
        return 0
    point = _POINT.format(lon="%(lon)s::double precision", lat="%(lat)s::double precision")
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO app.transit_observations (fetch_id, item_index, kind, route_id, stop_id, pattern_key, stop_seq,
                vehicle_no, run_key, location, remaining_seconds, eta_basis_at, predicted_arrival_at,
                source_generated_at, collected_at, details)
            VALUES (%(fetch_id)s, %(item_index)s, %(kind)s, %(route_id)s, %(stop_id)s, %(pattern_key)s, %(stop_seq)s,
                %(vehicle_no)s, %(run_key)s,
                CASE WHEN %(lat)s::double precision IS NULL OR %(lon)s::double precision IS NULL THEN NULL
                     ELSE {point} END,
                %(remaining_seconds)s, %(eta_basis_at)s, %(predicted_arrival_at)s, %(source_generated_at)s,
                %(collected_at)s, %(details)s)
            """,
            [
                {
                    "fetch_id": r.fetch_id, "item_index": r.item_index, "kind": r.kind, "route_id": r.route_id,
                    "stop_id": r.stop_id, "pattern_key": r.pattern_key, "stop_seq": r.stop_seq,
                    "vehicle_no": r.vehicle_no, "run_key": r.run_key, "lat": r.lat, "lon": r.lon,
                    "remaining_seconds": r.remaining_seconds, "eta_basis_at": r.eta_basis_at,
                    "predicted_arrival_at": r.predicted_arrival_at, "source_generated_at": r.source_generated_at,
                    "collected_at": r.collected_at, "details": Jsonb(r.details),
                }
                for r in rows
            ],
        )
    return len(rows)


def arrival_events(conn: psycopg.Connection, pattern_keys: Sequence[str], since: datetime | None = None,
                   until: datetime | None = None) -> list[dict[str, Any]]:
    """도출된 도착 이벤트(kind=event, event_type=arrival). 회차가 확인된 것만 반환한다(구간 소요시간·사후 판정용)."""
    if not pattern_keys:
        return []
    rows = conn.execute(
        """
        SELECT id, pattern_key, stop_seq, run_key, quality, event_at, details
          FROM app.transit_observations
         WHERE kind = 'event' AND event_type = 'arrival' AND run_key IS NOT NULL
           AND pattern_key = ANY(%s)
           AND (%s::timestamptz IS NULL OR event_at >= %s::timestamptz)
           AND (%s::timestamptz IS NULL OR event_at <= %s::timestamptz)
         ORDER BY pattern_key, run_key, stop_seq
        """,
        (list(pattern_keys), since, since, until, until),
    ).fetchall()
    keys = ("id", "pattern_key", "stop_seq", "run_key", "quality", "event_at", "details")
    return [dict(zip(keys, row)) for row in rows]


def find_arrival_event(conn: psycopg.Connection, pattern_key: str, stop_seq: int, run_key: str) -> dict[str, Any] | None:
    """같은 패턴·경유 위치·회차의 도착 이벤트 하나(점 시각 우선). 범위 이벤트는 details의 하한·상한을 함께 준다."""
    row = conn.execute(
        """
        SELECT id, quality, event_at, details FROM app.transit_observations
         WHERE kind = 'event' AND event_type = 'arrival' AND pattern_key = %s AND stop_seq = %s AND run_key = %s
         ORDER BY (quality = 'exact') DESC, id
         LIMIT 1
        """,
        (pattern_key, stop_seq, run_key),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "quality": row[1], "event_at": row[2], "details": row[3]}


def observations_for_fetches(conn: psycopg.Connection, fetch_ids: Sequence[int]) -> list[dict[str, Any]]:
    if not fetch_ids:
        return []
    rows = conn.execute(
        """
        SELECT o.id, o.fetch_id, o.item_index, o.kind, r.external_id, st.external_id, o.pattern_key, o.stop_seq,
               o.vehicle_no, o.run_key, extensions.ST_Y(o.location::extensions.geometry),
               extensions.ST_X(o.location::extensions.geometry), o.remaining_seconds, o.eta_basis_at,
               o.predicted_arrival_at, o.source_generated_at, o.collected_at, o.details
          FROM app.transit_observations o
          LEFT JOIN app.routes r ON r.id = o.route_id
          LEFT JOIN app.stops st ON st.id = o.stop_id
         WHERE o.fetch_id = ANY(%s) AND o.kind <> 'event'
         ORDER BY o.fetch_id, o.item_index
        """,
        (list(fetch_ids),),
    ).fetchall()
    keys = ("id", "fetch_id", "item_index", "kind", "route_external_id", "stop_external_id", "pattern_key", "stop_seq",
            "vehicle_no", "run_key", "lat", "lon", "remaining_seconds", "eta_basis_at", "predicted_arrival_at",
            "source_generated_at", "collected_at", "details")
    return [dict(zip(keys, row)) for row in rows]


# ---------------------------------------------------------------- 앱 조회 (Phase 4A)

_STOP_COLUMNS = ("s.id, s.external_id, s.name, s.stop_no, extensions.ST_Y(s.location::extensions.geometry), "
                 "extensions.ST_X(s.location::extensions.geometry), s.is_active, s.provider")
_STOP_KEYS = ("id", "external_id", "name", "stop_no", "lat", "lon", "is_active", "provider")
_ROUTE_COLUMNS = ("r.id, r.external_id, r.route_no, r.route_type, r.start_name, r.end_name, r.is_active, "
                  "r.provider, r.service_info, r.shape_info, r.data_version")
_ROUTE_KEYS = ("id", "external_id", "route_no", "route_type", "start_name", "end_name", "is_active", "provider",
               "service_info", "shape_info", "data_version")


def static_publication(conn: psycopg.Connection) -> dict[str, Any]:
    """현재 공개본 버전(활성 routes의 data_version)과 마지막 공개 시각. 버전이 섞였으면 versions에 모두 담는다."""
    versions = current_static_versions(conn)
    published = conn.execute(
        "SELECT max(published_at) FROM app.api_fetches WHERE provider = %s AND api_kind IN "
        "('stops','routes','route_stops') AND published_at IS NOT NULL", (PROVIDER,)).fetchone()[0]
    return {"versions": versions, "published_at": published}


def route_shape_infos(conn: psycopg.Connection, external_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = sorted(set(external_ids))
    if not ids:
        return {}
    return dict(conn.execute(
        "SELECT external_id, shape_info FROM app.routes WHERE provider = %s AND external_id = ANY(%s)",
        (PROVIDER, ids)).fetchall())


def stop_by_id(conn: psycopg.Connection, stop_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_STOP_COLUMNS} FROM app.stops s WHERE s.id = %s", (stop_id,)).fetchone()
    return None if row is None else dict(zip(_STOP_KEYS, row))


def stop_by_external_id(conn: psycopg.Connection, provider: str, external_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_STOP_COLUMNS} FROM app.stops s WHERE s.provider = %s AND s.external_id = %s",
                       (provider, external_id)).fetchone()
    return None if row is None else dict(zip(_STOP_KEYS, row))


def stop_route_visits(conn: psycopg.Connection, stop_id: int) -> list[dict[str, Any]]:
    """정류소를 지나는 활성 노선의 경유 위치. 한 패턴이 여러 번 지나면 seq별로 따로 반환한다."""
    rows = conn.execute(
        """
        SELECT r.id, r.route_no, r.route_type, r.external_id, rs.pattern_key, rs.direction_code, rs.seq,
               (SELECT st.name FROM app.route_stops last JOIN app.stops st ON st.id = last.stop_id
                 WHERE last.route_id = rs.route_id AND last.pattern_key = rs.pattern_key
                 ORDER BY last.seq DESC LIMIT 1) AS toward_name
          FROM app.route_stops rs JOIN app.routes r ON r.id = rs.route_id
         WHERE rs.stop_id = %s AND r.is_active
         ORDER BY r.route_no NULLS LAST, r.id, rs.pattern_key, rs.seq
        """,
        (stop_id,),
    ).fetchall()
    keys = ("route_id", "route_no", "route_type", "route_external_id", "pattern_key", "direction_code", "seq",
            "toward_name")
    return [dict(zip(keys, row)) for row in rows]


def route_by_id(conn: psycopg.Connection, route_id: int) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_ROUTE_COLUMNS} FROM app.routes r WHERE r.id = %s", (route_id,)).fetchone()
    return None if row is None else dict(zip(_ROUTE_KEYS, row))


def route_by_external_id(conn: psycopg.Connection, provider: str, external_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {_ROUTE_COLUMNS} FROM app.routes r WHERE r.provider = %s AND r.external_id = %s",
                       (provider, external_id)).fetchone()
    return None if row is None else dict(zip(_ROUTE_KEYS, row))


def routes_by_number(conn: psycopg.Connection, route_no: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT {_ROUTE_COLUMNS} FROM app.routes r WHERE r.is_active AND r.route_no = %s "
                        "ORDER BY r.id", (route_no,)).fetchall()
    return [dict(zip(_ROUTE_KEYS, row)) for row in rows]


def route_patterns(conn: psycopg.Connection, route_id: int) -> list[dict[str, Any]]:
    """노선의 현재 공개본 경유 위치를 패턴별로 묶은 요약(첫·마지막 정류소, 경유 위치 수, 방향 코드 원값)."""
    rows = conn.execute(
        """
        SELECT rs.pattern_key, rs.seq, rs.direction_code, st.id, st.name
          FROM app.route_stops rs JOIN app.stops st ON st.id = rs.stop_id
         WHERE rs.route_id = %s
         ORDER BY rs.pattern_key, rs.seq
        """,
        (route_id,),
    ).fetchall()
    patterns: dict[str, dict[str, Any]] = {}
    for key, seq, direction, stop_id, name in rows:
        item = patterns.setdefault(key, {"pattern_key": key, "stops": []})
        item["stops"].append({"seq": seq, "direction_code": direction, "stop_id": stop_id, "name": name})
    return list(patterns.values())


def pattern_stops(conn: psycopg.Connection, route_id: int, pattern_key: str,
                  after_seq: int | None = None) -> list[dict[str, Any]] | None:
    """패턴 경유 순서. 패턴이 없으면 None, afterSeq 뒤에 정류소가 없으면 []."""
    exists = conn.execute("SELECT 1 FROM app.route_stops WHERE route_id = %s AND pattern_key = %s LIMIT 1",
                          (route_id, pattern_key)).fetchone()
    if exists is None:
        return None
    rows = conn.execute(
        """
        SELECT rs.seq, rs.direction_code, rs.cumulative_distance_m, st.id, st.external_id, st.name,
               extensions.ST_Y(st.location::extensions.geometry), extensions.ST_X(st.location::extensions.geometry)
          FROM app.route_stops rs JOIN app.stops st ON st.id = rs.stop_id
         WHERE rs.route_id = %s AND rs.pattern_key = %s AND (%s::int IS NULL OR rs.seq > %s::int)
         ORDER BY rs.seq
        """,
        (route_id, pattern_key, after_seq, after_seq),
    ).fetchall()
    keys = ("seq", "direction_code", "cumulative_distance_m", "stop_id", "external_id", "name", "lat", "lon")
    return [dict(zip(keys, row)) for row in rows]


def internal_ids(conn: psycopg.Connection, route_external_ids: Iterable[str],
                 stop_external_ids: Iterable[str]) -> tuple[dict[str, int], dict[str, int]]:
    """원천 ID → 내부 ID(비활성 포함). 실시간 관측을 앱 응답의 내부 ID로 바꿀 때 쓴다."""
    routes = sorted(set(i for i in route_external_ids if i))
    stops = sorted(set(i for i in stop_external_ids if i))
    route_map = dict(conn.execute("SELECT external_id, id FROM app.routes WHERE provider = %s AND "
                                  "external_id = ANY(%s)", (PROVIDER, routes)).fetchall()) if routes else {}
    stop_map = dict(conn.execute("SELECT external_id, id FROM app.stops WHERE provider = %s AND "
                                 "external_id = ANY(%s)", (PROVIDER, stops)).fetchall()) if stops else {}
    return route_map, stop_map


# ---------------------------------------------------------------- 화면 조회 수집 수요 (Phase 4A 후속 보완)

def register_view_target(conn: psycopg.Connection, target_key: str, ttl_seconds: float) -> datetime:
    """화면 수요를 DB 시계(now()) 기준 만료 시각으로 등록·연장한다. 동시 조회가 와도 한 행이며 더 늦은 만료를 유지한다."""
    return conn.execute(
        """
        INSERT INTO app.realtime_view_targets AS t (target_key, expires_at)
        VALUES (%s, now() + make_interval(secs => %s))
        ON CONFLICT (target_key) DO UPDATE SET expires_at = GREATEST(t.expires_at, EXCLUDED.expires_at)
        RETURNING expires_at
        """,
        (target_key, float(ttl_seconds)),
    ).fetchone()[0]


def view_targets_table_exists(conn: psycopg.Connection) -> bool:
    """마이그레이션 10 적용 여부. 적용 전 DB에서는 워커가 화면 수요만 건너뛰고 기존 수집을 계속한다."""
    return conn.execute("SELECT to_regclass('app.realtime_view_targets') IS NOT NULL").fetchone()[0]


def active_view_target_keys(conn: psycopg.Connection, now: datetime) -> list[str]:
    if not view_targets_table_exists(conn):
        return []
    rows = conn.execute("SELECT target_key FROM app.realtime_view_targets WHERE expires_at > %s ORDER BY target_key",
                        (now,)).fetchall()
    return [row[0] for row in rows]


def delete_expired_view_targets(conn: psycopg.Connection, now: datetime) -> int:
    if not view_targets_table_exists(conn):
        return 0
    return conn.execute("DELETE FROM app.realtime_view_targets WHERE expires_at <= %s", (now,)).rowcount
