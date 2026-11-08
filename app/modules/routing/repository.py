"""검색(app.route_searches)과 후보(app.route_candidates)의 저장·조회.

적용 DDL: 서버 deployment/migrations/05_route_searches.sql·06_route_candidates.sql. 연결·트랜잭션은 호출자가 관리한다.
검색 행은 먼저 processing으로 만들어 필요 실시간 대상을 등록하고, 평가·추천이 끝나면 상태·모드·후보 전체를
한 트랜잭션에서 확정한다. 확정은 processing 행에만 적용되므로 같은 검색을 다시 확정하거나 섞어 저장하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_POINT = "extensions.ST_SetSRID(extensions.ST_MakePoint(%({lon})s::double precision, %({lat})s::double precision), 4326)::extensions.geography"


def create_search(
    conn: psycopg.Connection,
    *,
    source: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    place_info: dict[str, Any],
    start_at: datetime,
    static_version: UUID,
    config_version: str,
    params: dict[str, Any],
    summary: dict[str, Any],
    expires_at: datetime,
    user_id: UUID | None = None,
    journey_id: UUID | None = None,
) -> UUID:
    """processing 상태의 검색을 만든다. 비교 모드는 확정 전까지 초기값 nominal이다(DDL NOT NULL)."""
    return conn.execute(
        f"""
        INSERT INTO app.route_searches (user_id, journey_id, source, origin, destination, place_info, start_at,
            static_version, config_version, params, ranking_mode, status, summary, expires_at)
        VALUES (%(user_id)s, %(journey_id)s, %(source)s, {_POINT.format(lon='olon', lat='olat')},
            {_POINT.format(lon='dlon', lat='dlat')}, %(place_info)s, %(start_at)s, %(static_version)s,
            %(config_version)s, %(params)s, 'nominal', 'processing', %(summary)s, %(expires_at)s)
        RETURNING id
        """,
        {"user_id": user_id, "journey_id": journey_id, "source": source, "olat": origin[0], "olon": origin[1],
         "dlat": destination[0], "dlon": destination[1], "place_info": Jsonb(place_info), "start_at": start_at,
         "static_version": static_version, "config_version": config_version, "params": Jsonb(params),
         "summary": Jsonb(summary), "expires_at": expires_at},
    ).fetchone()[0]


def lock_processing_search(conn: psycopg.Connection, search_id: UUID) -> bool:
    """확정할 검색 행을 잠근다. processing이 아니면(이미 확정·실패) False."""
    row = conn.execute(
        "SELECT status FROM app.route_searches WHERE id = %s FOR UPDATE", (search_id,)
    ).fetchone()
    return row is not None and row[0] == "processing"


def finalize_search(
    conn: psycopg.Connection, search_id: UUID, *, status: str, ranking_mode: str, summary: dict[str, Any],
) -> None:
    conn.execute(
        "UPDATE app.route_searches SET status = %s, ranking_mode = %s, summary = %s "
        "WHERE id = %s AND status = 'processing'",
        (status, ranking_mode, Jsonb(summary), search_id),
    )


def insert_candidates(conn: psycopg.Connection, search_id: UUID, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO app.route_candidates (search_id, static_route_key, option_key, legs, boarding_count,
                walking_seconds, nominal_total_seconds, expected_total_seconds, journey_probability, time_status,
                nominally_feasible, evaluation, ranking_mode, rank_no, evaluated_at)
            VALUES (%(search_id)s, %(static_route_key)s, %(option_key)s, %(legs)s, %(boarding_count)s,
                %(walking_seconds)s, %(nominal_total_seconds)s, %(expected_total_seconds)s, %(journey_probability)s,
                %(time_status)s, %(nominally_feasible)s, %(evaluation)s, %(ranking_mode)s, %(rank_no)s,
                %(evaluated_at)s)
            """,
            [{**row, "search_id": search_id, "legs": Jsonb(row["legs"]), "evaluation": Jsonb(row["evaluation"])}
             for row in rows],
        )
    return len(rows)


def mark_search_error(conn: psycopg.Connection, search_id: UUID, summary_patch: dict[str, Any]) -> None:
    """확정 전에 실패한 검색을 error로 남긴다. 이미 확정된 검색은 바꾸지 않는다."""
    conn.execute(
        "UPDATE app.route_searches SET status = 'error', summary = summary || %s "
        "WHERE id = %s AND status = 'processing'",
        (Jsonb(summary_patch), search_id),
    )


def read_search(conn: psycopg.Connection, search_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, user_id, journey_id, source, extensions.ST_Y(origin::extensions.geometry),
               extensions.ST_X(origin::extensions.geometry), extensions.ST_Y(destination::extensions.geometry),
               extensions.ST_X(destination::extensions.geometry), place_info, start_at, static_version,
               config_version, params, ranking_mode, status, summary, expires_at, created_at
          FROM app.route_searches WHERE id = %s
        """,
        (search_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "user_id", "journey_id", "source", "origin_lat", "origin_lon", "destination_lat",
            "destination_lon", "place_info", "start_at", "static_version", "config_version", "params",
            "ranking_mode", "status", "summary", "expires_at", "created_at")
    return dict(zip(keys, row))


def read_candidates(conn: psycopg.Connection, search_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, static_route_key, option_key, legs, boarding_count, walking_seconds, nominal_total_seconds,
               expected_total_seconds, journey_probability, time_status, nominally_feasible, evaluation,
               ranking_mode, rank_no, evaluated_at
          FROM app.route_candidates WHERE search_id = %s
         ORDER BY rank_no NULLS LAST, static_route_key, option_key
        """,
        (search_id,),
    ).fetchall()
    keys = ("id", "static_route_key", "option_key", "legs", "boarding_count", "walking_seconds",
            "nominal_total_seconds", "expected_total_seconds", "journey_probability", "time_status",
            "nominally_feasible", "evaluation", "ranking_mode", "rank_no", "evaluated_at")
    return [dict(zip(keys, row)) for row in rows]


def active_search_targets(conn: psycopg.Connection, now: datetime) -> list[dict[str, Any]]:
    """유효기간 안의 검색이 등록한 실시간 수집 대상. 실패(error) 검색은 제외한다."""
    rows = conn.execute(
        """
        SELECT id, summary->'realtime_targets' FROM app.route_searches
         WHERE expires_at > %s AND status <> 'error' AND summary ? 'realtime_targets'
        """,
        (now,),
    ).fetchall()
    return [{"search_id": sid, "targets": targets or {}} for sid, targets in rows]


# ---------------------------------------------------------------- 앱 검색 작업 (Phase 4B)

def update_processing_plan(conn: psycopg.Connection, search_id: UUID, static_version: UUID,
                           summary: dict[str, Any]) -> bool:
    """워커가 후보를 만든 뒤 필요 실시간 대상·실제 사용한 정적 공개본을 processing 행에 기록한다."""
    row = conn.execute(
        "UPDATE app.route_searches SET static_version = %s, summary = summary || %s "
        "WHERE id = %s AND status = 'processing' RETURNING id",
        (static_version, Jsonb(summary), search_id),
    ).fetchone()
    return row is not None


def claim_queued_search(conn: psycopg.Connection, now: datetime, *, lease_seconds: float,
                        max_attempts: int) -> dict[str, Any] | None:
    """임대가 없거나 지난 앱 검색 하나를 가져간다(FOR UPDATE SKIP LOCKED). 호출자는 바로 커밋한 뒤 계산한다.

    반환: {"search_id", "action": "run"|"expired"|"exhausted", "attempts"}. 만료·시도 소진은 error로 끝낸다.
    """
    row = conn.execute(
        """
        SELECT id, summary, expires_at FROM app.route_searches
         WHERE source = 'app' AND status = 'processing'
           AND COALESCE((summary->'job'->>'lease_until')::timestamptz, '-infinity'::timestamptz) <= %s
         ORDER BY created_at, id
         LIMIT 1
         FOR UPDATE SKIP LOCKED
        """,
        (now,),
    ).fetchone()
    if row is None:
        return None
    search_id, summary, expires_at = row
    job = dict((summary or {}).get("job") or {})
    attempts = int(job.get("attempts") or 0)
    if expires_at <= now:
        job.update(state="failed", failed_at=now.isoformat(), reason="search_expired_before_processing")
        conn.execute("UPDATE app.route_searches SET status = 'error', summary = summary || %s WHERE id = %s",
                     (Jsonb({"job": job, "error": "search_expired_before_processing"}), search_id))
        return {"search_id": search_id, "action": "expired", "attempts": attempts}
    if attempts >= max_attempts:
        job.update(state="failed", failed_at=now.isoformat(), reason="search_job_attempts_exhausted")
        conn.execute("UPDATE app.route_searches SET status = 'error', summary = summary || %s WHERE id = %s",
                     (Jsonb({"job": job, "error": "search_job_attempts_exhausted"}), search_id))
        return {"search_id": search_id, "action": "exhausted", "attempts": attempts}
    job.update(state="running", attempts=attempts + 1, claimed_at=now.isoformat(),
               lease_until=(now + timedelta(seconds=lease_seconds)).isoformat())
    conn.execute("UPDATE app.route_searches SET summary = summary || %s WHERE id = %s",
                 (Jsonb({"job": job}), search_id))
    return {"search_id": search_id, "action": "run", "attempts": attempts + 1}


def read_user_search(conn: psycopg.Connection, search_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    """사용자 소유의 앱 검색만 반환한다. 다른 사용자·CLI·워커 검색은 None(404와 같게 처리)."""
    row = read_search(conn, search_id)
    if row is None or row["user_id"] != user_id or row["source"] != "app":
        return None
    return row


def read_candidate(conn: psycopg.Connection, candidate_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, search_id, static_route_key, option_key, legs, boarding_count, walking_seconds,
               nominal_total_seconds, expected_total_seconds, journey_probability, time_status, nominally_feasible,
               evaluation, ranking_mode, rank_no, evaluated_at
          FROM app.route_candidates WHERE id = %s
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "search_id", "static_route_key", "option_key", "legs", "boarding_count", "walking_seconds",
            "nominal_total_seconds", "expected_total_seconds", "journey_probability", "time_status",
            "nominally_feasible", "evaluation", "ranking_mode", "rank_no", "evaluated_at")
    return dict(zip(keys, row))
