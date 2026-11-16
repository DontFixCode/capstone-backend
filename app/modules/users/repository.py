"""회원정보(app.profiles)의 조회·저장. 적용 DDL: migrations/02_profiles.sql·09_profiles_soft_delete.sql.

잠금 순서: 개인 기록을 바꾸는 트랜잭션은 profiles 행 → journeys 행 순서로 잠근다(탈퇴 접수와 같은 순서라 교착이 없다).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

_COLUMNS = "user_id, display_name, created_at, updated_at, deleted_at, auth_blocked_at"
_KEYS = ("user_id", "display_name", "created_at", "updated_at", "deleted_at", "auth_blocked_at")


def _row(row: tuple | None) -> dict[str, Any] | None:
    return None if row is None else dict(zip(_KEYS, row))


def get_profile(conn: psycopg.Connection, user_id: UUID, *, for_update: bool = False,
                for_share: bool = False) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if for_update else (" FOR SHARE" if for_share else "")
    return _row(conn.execute(f"SELECT {_COLUMNS} FROM app.profiles WHERE user_id = %s{lock}", (user_id,)).fetchone())


def insert_profile_if_absent(conn: psycopg.Connection, user_id: UUID) -> bool:
    """프로필이 없으면 만든다. 동시 요청이 와도 PK 충돌은 무시되므로 한 행만 남는다. 새로 만들었으면 True."""
    row = conn.execute(
        "INSERT INTO app.profiles (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
        (user_id,),
    ).fetchone()
    return row is not None


def insert_deleted_profile_if_absent(conn: psycopg.Connection, user_id: UUID, deleted_at: datetime) -> bool:
    """프로필을 한 번도 만들지 않은 계정의 탈퇴: 탈퇴 상태로 바로 만든다(이후 자동 생성 경로가 활성 프로필을 만들 수 없음)."""
    row = conn.execute(
        "INSERT INTO app.profiles (user_id, deleted_at) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING "
        "RETURNING user_id", (user_id, deleted_at)).fetchone()
    return row is not None


def mark_deleted(conn: psycopg.Connection, user_id: UUID, deleted_at: datetime) -> dict[str, Any] | None:
    """처음 탈퇴 접수 시각만 기록한다. 이미 탈퇴한 계정은 바꾸지 않는다(None)."""
    return _row(conn.execute(
        f"UPDATE app.profiles SET deleted_at = %s, updated_at = %s WHERE user_id = %s AND deleted_at IS NULL "
        f"RETURNING {_COLUMNS}", (deleted_at, deleted_at, user_id)).fetchone())


def mark_auth_blocked(conn: psycopg.Connection, user_id: UUID, blocked_at: datetime) -> dict[str, Any] | None:
    """Auth 신규 로그인 차단 확인 시각을 한 번만 기록한다. 차단 시각은 탈퇴 접수보다 앞설 수 없다(CHECK)."""
    return _row(conn.execute(
        f"""
        UPDATE app.profiles SET auth_blocked_at = GREATEST(%s::timestamptz, deleted_at)
         WHERE user_id = %s AND deleted_at IS NOT NULL AND auth_blocked_at IS NULL
        RETURNING {_COLUMNS}
        """, (blocked_at, user_id)).fetchone())


def pending_auth_blocks(conn: psycopg.Connection, *, limit: int) -> list[UUID]:
    """탈퇴했지만 Auth 차단을 확인하지 못한 계정(재시도 대상). 오래 기다린 계정부터."""
    rows = conn.execute(
        "SELECT user_id FROM app.profiles WHERE deleted_at IS NOT NULL AND auth_blocked_at IS NULL "
        "ORDER BY deleted_at, user_id LIMIT %s", (limit,)).fetchall()
    return [r[0] for r in rows]


def update_display_name(conn: psycopg.Connection, user_id: UUID, display_name: str | None) -> dict[str, Any] | None:
    """값이 실제로 바뀔 때만 updated_at을 갱신한다."""
    return _row(conn.execute(
        f"""
        UPDATE app.profiles
           SET display_name = %(name)s,
               updated_at = CASE WHEN display_name IS DISTINCT FROM %(name)s THEN now() ELSE updated_at END
         WHERE user_id = %(uid)s
        RETURNING {_COLUMNS}
        """,
        {"name": display_name, "uid": user_id},
    ).fetchone())
