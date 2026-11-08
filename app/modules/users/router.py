"""회원정보 HTTP API (/api/v1/me). 탈퇴(DELETE)는 Phase 6 범위다."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.core.dependencies import current_user, get_db
from app.core.exceptions import ApiError, unauthorized
from app.core.security import AuthUser
from app.db.session import Database
from app.modules.users import service
from app.modules.users.schemas import ProfilePatch, ProfileResponse

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
