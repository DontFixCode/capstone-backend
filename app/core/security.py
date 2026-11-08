"""Supabase Auth 액세스 토큰 검증.

앱은 Supabase Auth(GoTrue)로 가입·로그인하고 액세스 토큰을 FastAPI에 보낸다. 이 모듈은 토큰의 서명·issuer·audience·
만료·sub를 검증해 사용자 ID를 정한다. 요청 본문의 사용자 ID는 쓰지 않는다.

- 서명: 이 저장소의 Supabase 구성(docker-compose.yml)은 GoTrue가 GOTRUE_JWT_SECRET으로 HS256 서명한다.
  허용 알고리즘은 HS256 하나로 고정하며 헤더의 alg(none 등)를 신뢰하지 않는다. 비대칭 키(JWT_KEYS) 서명은 아직 받지 않는다.
- iss: GOTRUE_JWT_ISSUER(=API_EXTERNAL_URL), aud: GOTRUE_JWT_AUD(=authenticated), sub: auth.users.id(UUID).
- role이 authenticated인 사용자 토큰만 받는다(anon·service_role API 키는 사용자 요청으로 쓰지 않는다).
값은 .env.api의 BACKEND_AUTH_JWT_*가 우선이고, 없으면 루트 .env(Supabase 설정)의 JWT_SECRET·API_EXTERNAL_URL을 쓴다.
토큰과 비밀값은 로그·예외 메시지에 넣지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import jwt
from dotenv import dotenv_values

if TYPE_CHECKING:
    from app.core.config import Settings

SUPABASE_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
ALGORITHMS = ("HS256",)
USER_ROLE = "authenticated"


class AuthConfigError(RuntimeError):
    """토큰 검증에 필요한 설정이 없다. 메시지에는 설정 이름만 담는다."""


class AuthError(Exception):
    """토큰이 없거나 유효하지 않다. code는 응답 오류 코드다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthUser:
    user_id: UUID
    role: str
    expires_at: datetime
    email: str | None = None
    session_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class TokenVerifier:
    def __init__(self, secret: str, *, issuer: str, audience: str, leeway_seconds: float = 0.0) -> None:
        if not secret:
            raise AuthConfigError("토큰 검증 비밀값이 없습니다.")
        if not issuer:
            raise AuthConfigError("토큰 issuer 설정이 없습니다.")
        self._secret = secret
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway_seconds

    def __repr__(self) -> str:  # 비밀값을 표시하지 않는다.
        return f"TokenVerifier(issuer={self.issuer!r}, audience={self.audience!r})"

    def verify(self, token: str) -> AuthUser:
        if not token or token.count(".") != 2:
            raise AuthError("INVALID_TOKEN", "액세스 토큰 형식이 잘못됐습니다.")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("INVALID_TOKEN", "액세스 토큰 형식이 잘못됐습니다.") from None
        if header.get("alg") not in ALGORITHMS:
            raise AuthError("INVALID_TOKEN", "지원하지 않는 토큰 서명 방식입니다.")
        try:
            claims = jwt.decode(
                token, self._secret, algorithms=list(ALGORITHMS), audience=self.audience, issuer=self.issuer,
                leeway=self.leeway, options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("TOKEN_EXPIRED", "액세스 토큰이 만료됐습니다.") from None
        except (jwt.InvalidAudienceError, jwt.InvalidIssuerError):
            raise AuthError("INVALID_TOKEN", "이 서비스용 액세스 토큰이 아닙니다.") from None
        except jwt.PyJWTError:
            raise AuthError("INVALID_TOKEN", "액세스 토큰이 유효하지 않습니다.") from None
        if claims.get("role") != USER_ROLE:
            raise AuthError("INVALID_TOKEN", "사용자 액세스 토큰이 아닙니다.")
        try:
            user_id = UUID(str(claims["sub"]))
        except (ValueError, KeyError):
            raise AuthError("INVALID_TOKEN", "토큰의 사용자 ID가 올바르지 않습니다.") from None
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        session = claims.get("session_id") if isinstance(claims.get("session_id"), str) else None
        return AuthUser(user_id=user_id, role=claims["role"],
                        expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
                        email=email, session_id=session, claims=claims)


def load_verifier(settings: Settings, env_file: Path | None = None) -> TokenVerifier:
    """설정(.env.api) → 루트 .env 순으로 비밀값·issuer를 찾는다. 없으면 누락된 설정 이름을 알려준다."""
    env = {k: (v or "").strip() for k, v in dotenv_values(env_file or SUPABASE_ENV_FILE).items()}
    secret = settings.auth_jwt_secret.get_secret_value() if settings.auth_jwt_secret else env.get("JWT_SECRET", "")
    issuer = settings.auth_jwt_issuer or env.get("API_EXTERNAL_URL", "")
    missing = []
    if not secret:
        missing.append(".env.api BACKEND_AUTH_JWT_SECRET 또는 .env JWT_SECRET")
    if not issuer:
        missing.append(".env.api BACKEND_AUTH_JWT_ISSUER 또는 .env API_EXTERNAL_URL")
    if missing:
        raise AuthConfigError("토큰 검증 설정이 없습니다: " + ", ".join(missing))
    return TokenVerifier(secret, issuer=issuer, audience=settings.auth_jwt_audience)


def bearer_token(value: str | None) -> str:
    """Authorization 헤더 값에서 Bearer 토큰을 꺼낸다."""
    if not value:
        raise AuthError("UNAUTHENTICATED", "Authorization: Bearer 액세스 토큰이 필요합니다.")
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("UNAUTHENTICATED", "Authorization: Bearer 액세스 토큰이 필요합니다.")
    return token.strip()
