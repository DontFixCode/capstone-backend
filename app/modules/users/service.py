"""회원정보 조회·초기화·변경과 탈퇴 상태 확인.

- 프로필은 검증된 토큰의 sub(=auth.users.id)로만 찾는다. 첫 GET /api/v1/me에서 없으면 만들고, 동시 요청이 와도 한 행만 남긴다.
- deleted_at이 있으면 소프트 삭제된 계정이다. 프로필을 다시 만들거나 수정하지 않고 ACCOUNT_DELETED로 처리한다.
- 탈퇴 접수(DELETE /me)와 Auth 로그인 차단은 Phase 6 범위라 여기서 구현하지 않는다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from app.db.session import Database
from app.modules.users import repository


class AccountDeleted(Exception):
    """소프트 삭제된 계정."""


class AccountUnknown(Exception):
    """토큰의 사용자가 auth.users에 없다(계정이 물리 삭제됐거나 다른 인증 서버의 토큰)."""


class InvalidDisplayName(ValueError):
    """공백만 있는 표시 이름."""


def _check_active(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if profile is not None and profile["deleted_at"] is not None:
        raise AccountDeleted()
    return profile


def account_state(db: Database, user_id: UUID) -> dict[str, Any] | None:
    """프로필을 만들지 않고 상태만 확인한다. 탈퇴 계정이면 AccountDeleted, 프로필이 없으면 None."""
    with db.transaction() as conn:
        return _check_active(repository.get_profile(conn, user_id))


def ensure_profile(db: Database, user_id: UUID) -> dict[str, Any]:
    """활성 계정의 프로필을 조회하고, 없으면 같은 ID로 한 번만 만든다."""
    try:
        with db.transaction() as conn:
            profile = _check_active(repository.get_profile(conn, user_id))
            if profile is not None:
                return profile
            repository.insert_profile_if_absent(conn, user_id)
            # 동시 요청이 먼저 만들었으면 그 행을 읽는다(같은 트랜잭션에서 충돌 무시 후 재조회).
            return _check_active(repository.get_profile(conn, user_id))
    except psycopg.errors.ForeignKeyViolation:
        raise AccountUnknown() from None


def update_display_name(db: Database, user_id: UUID, value: str | None, *, provided: bool) -> dict[str, Any]:
    """문자열이면 변경, None이면 지움, 생략(provided=False)이면 유지. 공백만 있는 문자열은 거부한다."""
    if provided and value is not None and not value.strip():
        raise InvalidDisplayName()
    profile = ensure_profile(db, user_id)
    if not provided:
        return profile
    with db.transaction() as conn:
        current = _check_active(repository.get_profile(conn, user_id, for_update=True))
        if current is None:  # 방금 만든 행이 없을 수 없지만 방어적으로 처리
            raise AccountUnknown()
        return repository.update_display_name(conn, user_id, value)
