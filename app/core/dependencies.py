"""앱 API 공통 의존성: DB, 토큰 검증, 사용자 확인.

- 모든 앱 API(/api/v1/...)는 Bearer 액세스 토큰을 요구한다. 공개 여부가 정해지지 않은 places·stops·routes도 인증을 적용한다.
- active_user: 토큰 검증 + 탈퇴 계정 차단(프로필은 만들지 않음). 공개 교통정보 조회에 쓴다.
- profile_user: 토큰 검증 + 프로필 확보(없으면 한 번만 생성) + 탈퇴 계정 차단. 개인 리소스(검색·이동)에 쓴다.
DB·검증기는 app.state에 한 번 만들어 두며, 시험·별도 실행기는 app.state에 직접 주입할 수 있다.
"""

from __future__ import annotations

import logging
import threading

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.exceptions import ApiError, unauthorized
from app.core.security import AuthConfigError, AuthError, AuthUser, TokenVerifier, bearer_token, load_verifier
from app.db.session import Database, DbConfigError
from app.modules.users import service as user_service

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def database_for(app) -> Database:
    db = getattr(app.state, "db", None)
    if db is not None:
        return db
    with _lock:
        if getattr(app.state, "db", None) is None:
            try:
                app.state.db = Database.from_settings(app.state.settings)
            except DbConfigError as exc:
                logger.error("DB 접속 설정이 없습니다: %s", exc)
                raise ApiError(503, "DB_UNAVAILABLE", "DB 접속 설정이 없습니다.") from None
        return app.state.db


def verifier_for(app) -> TokenVerifier:
    verifier = getattr(app.state, "token_verifier", None)
    if verifier is not None:
        return verifier
    with _lock:
        if getattr(app.state, "token_verifier", None) is None:
            try:
                app.state.token_verifier = load_verifier(app.state.settings)
            except AuthConfigError as exc:
                logger.error("%s", exc)
                raise ApiError(503, "AUTH_NOT_CONFIGURED", "토큰 검증 설정이 없습니다.") from None
        return app.state.token_verifier


def get_db(request: Request) -> Database:
    return database_for(request.app)


def authenticate(verifier: TokenVerifier, authorization: str | None) -> AuthUser:
    try:
        return verifier.verify(bearer_token(authorization))
    except AuthError as exc:
        raise unauthorized(exc.code, exc.message) from None


# Swagger의 Authorize 버튼(HTTP Bearer)으로 토큰을 한 번만 입력하게 한다. 형식 오류·누락은 직접 401로 응답한다.
bearer_scheme = HTTPBearer(auto_error=False, description="Supabase Auth 액세스 토큰")


def current_user(request: Request,
                 credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> AuthUser:
    header = f"{credentials.scheme} {credentials.credentials}" if credentials else None
    return authenticate(verifier_for(request.app), header)


def check_account(db: Database, user: AuthUser, *, create_profile: bool) -> None:
    try:
        if create_profile:
            user_service.ensure_profile(db, user.user_id)
        else:
            user_service.account_state(db, user.user_id)
    except user_service.AccountDeleted:
        raise ApiError(403, "ACCOUNT_DELETED", "탈퇴 처리된 계정입니다.") from None
    except user_service.AccountUnknown:
        raise unauthorized("INVALID_TOKEN", "토큰의 사용자 계정을 찾을 수 없습니다.") from None


def active_user(user: AuthUser = Depends(current_user), db: Database = Depends(get_db)) -> AuthUser:
    check_account(db, user, create_profile=False)
    return user


def profile_user(user: AuthUser = Depends(current_user), db: Database = Depends(get_db)) -> AuthUser:
    check_account(db, user, create_profile=True)
    return user
