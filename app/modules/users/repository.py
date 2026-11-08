"""회원정보(app.profiles)의 조회·저장. 적용 DDL: migrations/02_profiles.sql·09_profiles_soft_delete.sql."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

_COLUMNS = "user_id, display_name, created_at, updated_at, deleted_at, auth_blocked_at"
_KEYS = ("user_id", "display_name", "created_at", "updated_at", "deleted_at", "auth_blocked_at")


def _row(row: tuple | None) -> dict[str, Any] | None:
    return None if row is None else dict(zip(_KEYS, row))


def get_profile(conn: psycopg.Connection, user_id: UUID, *, for_update: bool = False) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if for_update else ""
    return _row(conn.execute(f"SELECT {_COLUMNS} FROM app.profiles WHERE user_id = %s{lock}", (user_id,)).fetchone())


def insert_profile_if_absent(conn: psycopg.Connection, user_id: UUID) -> bool:
    """프로필이 없으면 만든다. 동시 요청이 와도 PK 충돌은 무시되므로 한 행만 남는다. 새로 만들었으면 True."""
    row = conn.execute(
        "INSERT INTO app.profiles (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
        (user_id,),
    ).fetchone()
    return row is not None


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
