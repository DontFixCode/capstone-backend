"""이동 세션(app.journeys)·이벤트(app.journey_events)의 조회·저장. 적용 DDL: migrations/07_journeys.sql·08_journey_events.sql.

연결·트랜잭션은 호출자가 관리한다. 사용자당 활성 이동 1개는 부분 UNIQUE 인덱스(journeys_one_active_per_user_idx)가 보장하고,
같은 입력 재전송은 UNIQUE(journey_id, idempotency_key)로 막는다. 이벤트는 추가만 하고 수정하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_POINT = ("extensions.ST_SetSRID(extensions.ST_MakePoint(%(lon)s::double precision, %(lat)s::double precision), "
          "4326)::extensions.geography")
_COLUMNS = """j.id, j.user_id, j.initial_search_id, j.selected_candidate_id, j.proposed_candidate_id,
       j.selected_snapshot, j.proposed_snapshot, j.lifecycle_status, j.movement_state, j.connection_state,
       j.current_progress, extensions.ST_Y(j.current_location::extensions.geometry),
       extensions.ST_X(j.current_location::extensions.geometry), j.location_observed_at, j.location_received_at,
       j.location_accuracy_m, j.last_connected_at, j.state_version, j.selection_version, j.next_recalc_at,
       j.result_version, j.guidance, j.notify_state, j.started_at, j.ended_at"""
_KEYS = ("id", "user_id", "initial_search_id", "selected_candidate_id", "proposed_candidate_id", "selected_snapshot",
         "proposed_snapshot", "lifecycle_status", "movement_state", "connection_state", "current_progress",
         "location_lat", "location_lon", "location_observed_at", "location_received_at", "location_accuracy_m",
         "last_connected_at", "state_version", "selection_version", "next_recalc_at", "result_version", "guidance",
         "notify_state", "started_at", "ended_at")
_EVENT_KEYS = ("id", "journey_id", "idempotency_key", "event_type", "source", "occurred_at", "received_at",
               "state_version_after", "payload")


def insert_journey(conn: psycopg.Connection, *, user_id: UUID, search_id: UUID, candidate_id: UUID,
                   snapshot: dict[str, Any], movement_state: str, progress: dict[str, Any],
                   now: datetime) -> UUID:
    """활성 이동을 만든다. 같은 사용자의 활성 이동이 있으면 UniqueViolation. 최초 재평가를 바로 예약한다."""
    return conn.execute(
        """
        INSERT INTO app.journeys (user_id, initial_search_id, selected_candidate_id, selected_snapshot,
            lifecycle_status, movement_state, connection_state, current_progress, next_recalc_at, started_at)
        VALUES (%s, %s, %s, %s, 'active', %s, 'unknown', %s, %s, %s)
        RETURNING id
        """,
        (user_id, search_id, candidate_id, Jsonb(snapshot), movement_state, Jsonb(progress), now, now),
    ).fetchone()[0]


def active_journey_id(conn: psycopg.Connection, user_id: UUID) -> UUID | None:
    row = conn.execute("SELECT id FROM app.journeys WHERE user_id = %s AND lifecycle_status = 'active'",
                       (user_id,)).fetchone()
    return row[0] if row else None


def get_journey(conn: psycopg.Connection, journey_id: UUID, user_id: UUID | None = None, *,
                for_update: bool = False) -> dict[str, Any] | None:
    where = "j.id = %s" + (" AND j.user_id = %s" if user_id is not None else "")
    params = (journey_id, user_id) if user_id is not None else (journey_id,)
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(f"SELECT {_COLUMNS} FROM app.journeys j WHERE {where}{lock}", params).fetchone()
    return None if row is None else dict(zip(_KEYS, row))


def find_event(conn: psycopg.Connection, journey_id: UUID, idempotency_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, journey_id, idempotency_key, event_type, source, occurred_at, received_at, state_version_after, "
        "payload FROM app.journey_events WHERE journey_id = %s AND idempotency_key = %s",
        (journey_id, idempotency_key)).fetchone()
    return None if row is None else dict(zip(_EVENT_KEYS, row))


def get_event(conn: psycopg.Connection, journey_id: UUID, event_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, journey_id, idempotency_key, event_type, source, occurred_at, received_at, state_version_after, "
        "payload FROM app.journey_events WHERE journey_id = %s AND id = %s", (journey_id, event_id)).fetchone()
    return None if row is None else dict(zip(_EVENT_KEYS, row))


def events(conn: psycopg.Connection, journey_id: UUID, event_type: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, journey_id, idempotency_key, event_type, source, occurred_at, received_at, state_version_after, "
        "payload FROM app.journey_events WHERE journey_id = %s AND (%s::text IS NULL OR event_type = %s::text) "
        "ORDER BY received_at, id", (journey_id, event_type, event_type)).fetchall()
    return [dict(zip(_EVENT_KEYS, row)) for row in rows]


def insert_event(conn: psycopg.Connection, *, journey_id: UUID, idempotency_key: str, event_type: str, source: str,
                 occurred_at: datetime | None, received_at: datetime, state_version_after: int,
                 payload: dict[str, Any], skip_existing: bool = False) -> int | None:
    """이벤트를 추가한다. skip_existing이면 같은 키가 있을 때 아무것도 바꾸지 않고 None(예측 스냅샷 고정용)."""
    conflict = " ON CONFLICT (journey_id, idempotency_key) DO NOTHING" if skip_existing else ""
    row = conn.execute(
        f"""
        INSERT INTO app.journey_events (journey_id, idempotency_key, event_type, source, occurred_at, received_at,
            state_version_after, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s){conflict}
        RETURNING id
        """,
        (journey_id, idempotency_key, event_type, source, occurred_at, received_at, state_version_after,
         Jsonb(payload)),
    ).fetchone()
    return row[0] if row else None


def update_state(conn: psycopg.Connection, journey_id: UUID, *, lifecycle_status: str, movement_state: str,
                 progress: dict[str, Any], state_version: int, ended_at: datetime | None,
                 next_recalc_at: datetime | None) -> None:
    conn.execute(
        """
        UPDATE app.journeys
           SET lifecycle_status = %s, movement_state = %s, current_progress = %s, state_version = %s,
               ended_at = %s, next_recalc_at = %s
         WHERE id = %s
        """,
        (lifecycle_status, movement_state, Jsonb(progress), state_version, ended_at, next_recalc_at, journey_id),
    )


def update_location(conn: psycopg.Connection, journey_id: UUID, *, lat: float, lon: float, observed_at: datetime,
                    received_at: datetime, accuracy_m: float | None, recalc_now: bool) -> bool:
    """관측 시각이 더 새로울 때만 반영한다(역순 위치는 최신값을 덮어쓰지 않음). 반영했으면 True."""
    row = conn.execute(
        f"""
        UPDATE app.journeys
           SET current_location = {_POINT}, location_observed_at = %(obs)s, location_received_at = %(recv)s,
               location_accuracy_m = %(acc)s,
               next_recalc_at = CASE WHEN %(recalc)s THEN LEAST(next_recalc_at, %(recv)s) ELSE next_recalc_at END
         WHERE id = %(id)s AND lifecycle_status = 'active'
           AND (location_observed_at IS NULL OR location_observed_at < %(obs)s)
        RETURNING id
        """,
        {"lat": lat, "lon": lon, "obs": observed_at, "recv": received_at, "acc": accuracy_m, "recalc": recalc_now,
         "id": journey_id},
    ).fetchone()
    return row is not None


def list_journeys(conn: psycopg.Connection, user_id: UUID, *, status: str | None,
                  before: tuple[datetime, UUID] | None, limit: int) -> list[dict[str, Any]]:
    """본인 이동을 시작 시각·ID 내림차순으로. before는 이전 페이지 마지막 항목(커서)."""
    rows = conn.execute(
        f"""
        SELECT {_COLUMNS} FROM app.journeys j
         WHERE j.user_id = %(uid)s AND (%(status)s::text IS NULL OR j.lifecycle_status = %(status)s::text)
           AND (%(bt)s::timestamptz IS NULL OR (j.started_at, j.id) < (%(bt)s::timestamptz, %(bid)s::uuid))
         ORDER BY j.started_at DESC, j.id DESC
         LIMIT %(n)s
        """,
        {"uid": user_id, "status": status, "bt": before[0] if before else None, "bid": before[1] if before else None,
         "n": limit},
    ).fetchall()
    return [dict(zip(_KEYS, row)) for row in rows]


# ---------------------------------------------------------------- 재평가(워커)

def claim_due_journeys(conn: psycopg.Connection, now: datetime, *, lease_seconds: float,
                       limit: int) -> list[dict[str, Any]]:
    """예약 시각이 지난 활성 이동을 가져가 예약을 임대 만료 시각으로 미룬다. 호출자는 바로 커밋한 뒤 계산한다."""
    lease = now + timedelta(seconds=lease_seconds)
    rows = conn.execute(
        """
        WITH due AS (
            SELECT id FROM app.journeys
             WHERE lifecycle_status = 'active' AND next_recalc_at <= %(now)s
             ORDER BY next_recalc_at, id
             LIMIT %(n)s
             FOR UPDATE SKIP LOCKED
        )
        UPDATE app.journeys j SET next_recalc_at = %(lease)s
          FROM due WHERE j.id = due.id
        RETURNING j.id, j.state_version, j.next_recalc_at
        """,
        {"now": now, "n": limit, "lease": lease},
    ).fetchall()
    return [{"id": r[0], "state_version": r[1], "leased_until": r[2]} for r in rows]


def save_guidance(conn: psycopg.Connection, journey_id: UUID, *, expected_state_version: int,
                  leased_until: datetime, guidance: dict[str, Any], next_recalc_at: datetime) -> int | None:
    """계산에 쓴 상태 버전이 그대로이고 활성·탈퇴 전일 때만 저장한다. 결과 버전을 하나 올린다.

    계산 중 새 입력·위치가 예약을 바꿨으면(next_recalc_at이 임대 값과 다름) 그 예약을 늦추지 않는다.
    """
    row = conn.execute(
        """
        UPDATE app.journeys j
           SET guidance = jsonb_set(%(g)s::jsonb, '{result_version}', to_jsonb(j.result_version + 1)),
               result_version = j.result_version + 1,
               next_recalc_at = CASE WHEN j.next_recalc_at = %(lease)s THEN %(next)s ELSE j.next_recalc_at END
          FROM app.profiles p
         WHERE j.id = %(id)s AND p.user_id = j.user_id AND p.deleted_at IS NULL
           AND j.state_version = %(sv)s AND j.lifecycle_status = 'active'
        RETURNING j.result_version
        """,
        {"g": Jsonb(guidance), "lease": leased_until, "next": next_recalc_at, "id": journey_id,
         "sv": expected_state_version},
    ).fetchone()
    return row[0] if row else None


def release_stale_lease(conn: psycopg.Connection, journey_id: UUID, *, leased_until: datetime,
                        next_recalc_at: datetime) -> None:
    """계산 결과를 버렸을 때 임대 예약이 그대로면 다음 확인 시각으로 되돌린다(새 입력의 예약은 건드리지 않음)."""
    conn.execute("UPDATE app.journeys SET next_recalc_at = %s WHERE id = %s AND lifecycle_status = 'active' "
                 "AND next_recalc_at = %s", (next_recalc_at, journey_id, leased_until))


def active_journey_snapshots(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """공유 수집 대상 취합용: 재평가가 예약된 활성 이동의 선택 스냅샷·진행 상태."""
    rows = conn.execute(
        "SELECT id, selected_snapshot, current_progress, movement_state FROM app.journeys "
        "WHERE lifecycle_status = 'active' AND next_recalc_at IS NOT NULL").fetchall()
    return [{"id": r[0], "selected_snapshot": r[1], "current_progress": r[2], "movement_state": r[3]} for r in rows]


# ---------------------------------------------------------------- 연결 상태(WebSocket)

def set_connection(conn: psycopg.Connection, journey_id: UUID, state: str, now: datetime) -> None:
    """앱 연결 상태만 기록한다. 이동 생명주기·재평가 예약·수집 대상은 바꾸지 않는다."""
    conn.execute(
        "UPDATE app.journeys SET connection_state = %s, "
        "last_connected_at = CASE WHEN %s = 'connected' THEN %s ELSE last_connected_at END WHERE id = %s",
        (state, state, now, journey_id))


def stream_versions(conn: psycopg.Connection, journey_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT j.state_version, j.result_version, j.lifecycle_status, p.deleted_at FROM app.journeys j "
        "JOIN app.profiles p ON p.user_id = j.user_id WHERE j.id = %s", (journey_id,)).fetchone()
    if row is None:
        return None
    return {"state_version": row[0], "result_version": row[1], "lifecycle_status": row[2], "deleted_at": row[3]}
