"""회원정보 HTTP API (/api/v1/me): 조회·초기화, 표시 이름 변경, 탈퇴(소프트 삭제) 접수."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.core.dependencies import auth_admin_factory, current_user, get_db
from app.core.exceptions import ApiError, unauthorized
from app.core.security import AuthUser
from app.db.session import Database
from app.modules.users import service
from app.modules.users.schemas import AccountDeletionResponse, ProfilePatch, ProfileResponse

router = APIRouter(prefix="/api/v1", tags=["me"])


def _translate(call):
    try:
        return call()
    except service.AccountDeleted:
        raise ApiError(403, "ACCOUNT_DELETED", "탈퇴 처리된 계정입니다.") from None
    except service.AccountUnknown:
        raise unauthorized("INVALID_TOKEN", "토큰의 사용자 계정을 찾을 수 없습니다.") from None
    except service.InvalidDisplayName:
        raise ApiError(400, "INVALID_DISPLAY_NAME", "표시 이름이 공백만으로 되어 있습니다.") from None


@router.get("/me", response_model=ProfileResponse)
async def get_me(user: AuthUser = Depends(current_user), db: Database = Depends(get_db)) -> ProfileResponse:
    profile = await run_in_threadpool(_translate, lambda: service.ensure_profile(db, user.user_id))
    return ProfileResponse.from_row(profile)


@router.patch("/me", response_model=ProfileResponse)
async def patch_me(body: ProfilePatch, user: AuthUser = Depends(current_user),
                   db: Database = Depends(get_db)) -> ProfileResponse:
    profile = await run_in_threadpool(_translate, lambda: service.update_display_name(
        db, user.user_id, body.displayName, provided=body.provided()))
    return ProfileResponse.from_row(profile)


@router.delete("/me", status_code=202, response_model=AccountDeletionResponse)
async def delete_me(request: Request, user: AuthUser = Depends(current_user),
                    db: Database = Depends(get_db)) -> AccountDeletionResponse:
    """탈퇴 접수(소프트 삭제). 이미 탈퇴한 계정의 재요청도 202로 기존 접수를 반환하고 Auth 차단만 다시 시도한다.

    탈퇴 계정 차단(403)을 거치지 않는 유일한 개인 API다. 접수 직후부터 다른 개인 API·WebSocket은 403/4403이다.
    """
    factory = auth_admin_factory(request.app)
    outcome = await run_in_threadpool(_translate, lambda: service.delete_account(
        db, user.user_id, now=datetime.now(timezone.utc), admin_factory=factory))
    return AccountDeletionResponse.from_outcome(outcome)
