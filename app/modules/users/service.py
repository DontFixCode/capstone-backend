"""회원정보 조회·초기화·변경, 탈퇴(소프트 삭제) 접수와 Auth 신규 로그인 차단.

- 프로필은 검증된 토큰의 sub(=auth.users.id)로만 찾는다. 첫 GET /api/v1/me에서 없으면 만들고, 동시 요청이 와도 한 행만 남긴다.
- deleted_at이 있으면 소프트 삭제된 계정이다. 프로필을 다시 만들거나 수정하지 않고 ACCOUNT_DELETED로 처리한다.
- 탈퇴(DELETE /me): 한 트랜잭션에서 profiles.deleted_at을 처음 한 번 기록하고 활성 이동을 cancelled로 전환한다(재평가 예약·
  이동 수집 대상 해제). 커밋된 deleted_at이 서버 접근 차단의 근거이므로 외부 Auth 호출보다 먼저 영속화한다.
  그다음 트랜잭션 밖에서 Supabase Auth 관리자 기능으로 신규 로그인을 막고, 성공하면 auth_blocked_at을 기록한다.
  실패·미설정이면 auth_blocked_at이 NULL로 남아 재요청·워커가 다시 시도한다. Auth 사용자·UUID·이메일·개인 기록은 보존한다.
- 표시용 ID: 탈퇴 계정은 원래 user_id 문자열 뒤에 _deleted를 붙여 표시한다. FK·토큰 검증에는 원래 UUID를 쓴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from app.db.session import Database
from app.integrations.supabase_auth import BlockResult
from app.modules.journeys import service as journey_service
from app.modules.users import repository

logger = logging.getLogger(__name__)
DELETED_SUFFIX = "_deleted"


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


def display_user_id(profile: dict[str, Any]) -> str:
    """화면·응답 표시용 ID. 탈퇴 계정만 _deleted를 붙이고 실제 UUID는 바꾸지 않는다."""
    uid = str(profile["user_id"])
    return uid + DELETED_SUFFIX if profile.get("deleted_at") is not None else uid


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
            # 동시에 탈퇴가 먼저 커밋됐으면 그 행이 보이므로 탈퇴 계정으로 처리된다(활성 프로필을 다시 만들지 않음).
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


# ---------------------------------------------------------------- 탈퇴

@dataclass
class DeletionOutcome:
    profile: dict[str, Any]
    first_request: bool
    cancelled_journey_ids: list[UUID] = field(default_factory=list)
    auth_block: str = "pending"  # blocked / pending / not_configured / failed


AdminFactory = Callable[[], Any]


def delete_account(db: Database, user_id: UUID, *, now: datetime,
                   admin_factory: AdminFactory | None = None) -> DeletionOutcome:
    """탈퇴 접수. 첫 요청·재요청 모두 같은 결과 형식이며, 재요청은 기존 접수 시각을 유지하고 Auth 차단만 다시 시도한다."""
    try:
        with db.transaction() as conn:
            profile = repository.get_profile(conn, user_id, for_update=True)
            first = False
            if profile is None:
                # 프로필을 만든 적 없는 계정: 탈퇴 상태로 바로 만든다. 동시 요청이 먼저 만들었으면 그 행을 잠그고 이어간다.
                first = repository.insert_deleted_profile_if_absent(conn, user_id, now)
                profile = repository.get_profile(conn, user_id, for_update=True)
            if profile["deleted_at"] is None:
                profile = repository.mark_deleted(conn, user_id, now)
                first = True
            # 재요청에서도 다시 확인한다: 탈퇴 상태에서는 새 활성 이동을 만들 수 없지만(프로필 잠금), 남은 것이 있으면 정리한다.
            cancelled = journey_service.cancel_for_account_deletion(conn, user_id, now=now)
    except psycopg.errors.ForeignKeyViolation:
        raise AccountUnknown() from None
    outcome = DeletionOutcome(profile=profile, first_request=first, cancelled_journey_ids=cancelled)
    logger.info("계정 탈퇴 접수 %s (첫 요청 %s, 취소한 이동 %d건)", user_id, first, len(cancelled))
    if profile["auth_blocked_at"] is not None:
        outcome.auth_block = "blocked"
        return outcome
    try:
        outcome.auth_block, updated = block_auth_login(db, user_id, admin_factory=admin_factory, now=now)
    except Exception:  # 탈퇴는 이미 커밋됐다. 차단 기록 실패는 대기 상태로 남겨 재요청·워커가 다시 시도한다.
        logger.exception("Auth 차단 처리 중 오류(사용자 %s). 대기 상태로 남깁니다.", user_id)
        outcome.auth_block = "failed"
        return outcome
    if updated is not None:
        outcome.profile = updated
    return outcome


def block_auth_login(db: Database, user_id: UUID, *, admin_factory: AdminFactory | None,
                     now: datetime) -> tuple[str, dict[str, Any] | None]:
    """Auth 관리자 기능으로 신규 로그인을 막고 성공하면 auth_blocked_at을 기록한다. DB 트랜잭션 밖에서 외부를 호출한다.

    반환: (blocked / not_configured / failed, 갱신된 프로필 또는 None). 실패해도 deleted_at은 그대로라 API 접근은 차단된다.
    """
    from app.integrations.supabase_auth import AuthAdminConfigError

    if admin_factory is None:
        return "not_configured", None
    try:
        admin = admin_factory()
    except AuthAdminConfigError as exc:
        logger.warning("Auth 신규 로그인 차단 보류(미설정): %s", exc)
        return "not_configured", None
    try:
        result: BlockResult = admin.block_new_logins(user_id)
    finally:
        admin.close()
    if not result.ok:
        return "failed", None
    with db.transaction() as conn:
        updated = repository.mark_auth_blocked(conn, user_id, now)
        if updated is None:  # 이미 다른 요청·워커가 기록했다.
            updated = repository.get_profile(conn, user_id)
    return "blocked", updated


def retry_pending_auth_blocks(db: Database, *, admin_factory: AdminFactory | None, clock: Callable[[], datetime],
                              limit: int) -> list[dict[str, Any]]:
    """차단 대기 계정(deleted_at 있음·auth_blocked_at 없음)을 다시 시도한다. 워커가 주기적으로 호출한다.

    관리자 설정이 없으면 외부 호출 없이 끝낸다(대기 상태 유지). 같은 사용자의 중복 차단 요청은 결과가 같아 안전하다.
    """
    with db.transaction() as conn:
        pending = repository.pending_auth_blocks(conn, limit=limit)
    if not pending:
        return []
    from app.integrations.supabase_auth import AuthAdminConfigError

    if admin_factory is None:
        return [{"user_id": str(uid), "status": "not_configured"} for uid in pending]
    try:
        admin = admin_factory()
    except AuthAdminConfigError as exc:
        logger.warning("Auth 차단 재시도 보류(미설정, 대기 %d건): %s", len(pending), exc)
        return [{"user_id": str(uid), "status": "not_configured"} for uid in pending]
    results = []
    try:
        for uid in pending:
            outcome = admin.block_new_logins(uid)
            if outcome.ok:
                with db.transaction() as conn:
                    repository.mark_auth_blocked(conn, uid, clock())
            results.append({"user_id": str(uid), "status": outcome.status, "http_status": outcome.http_status})
    finally:
        admin.close()
    logger.info("Auth 차단 재시도: %d건 중 성공 %d건", len(results), sum(r["status"] == "blocked" for r in results))
    return results
