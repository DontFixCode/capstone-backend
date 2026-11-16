"""Supabase Auth(GoTrue) 관리자 기능 연동 — 회원탈퇴 계정의 신규 로그인 차단.

회원가입·로그인·로그아웃·비밀번호 재설정은 앱이 Supabase Auth SDK로 직접 처리한다. 서버는 탈퇴 접수 뒤 관리자 API로
사용자에게 ban_duration을 걸어 새 로그인·토큰 갱신을 막는다(공식 auth.admin.updateUserById({ban_duration})와 같은 요청).
Auth 사용자를 물리 삭제하지 않으며 이메일·UUID는 그대로 남는다.

- 요청: PUT {admin_url}/admin/users/{user_id}, 본문 {"ban_duration": "<기간>"}. GoTrue는 GOTRUE_JWT_ADMIN_ROLES(service_role)
  토큰만 관리자 요청으로 받으므로 service_role 키를 Authorization: Bearer와 apikey(게이트웨이 경유 시) 헤더로 보낸다.
- admin_url: Docker 네트워크 안의 GoTrue(예: http://auth:9999) 또는 게이트웨이 경로(예: https://host/auth/v1). 근거 없는
  기본 주소를 두지 않는다. 비어 있으면 미설정이며 차단은 대기 상태로 남아 워커가 재시도한다.
- service_role 키: .env.api BACKEND_AUTH_SERVICE_ROLE_KEY → 루트 .env SERVICE_ROLE_KEY(서버 배포는 Supabase .env를 /app/.env로 연결).
- 키·응답 본문(이메일 등 개인정보 포함)은 로그·예외 메시지에 넣지 않는다. 결과에는 HTTP 상태와 분류만 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from dotenv import dotenv_values

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)
SUPABASE_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class AuthAdminConfigError(RuntimeError):
    """관리자 연동 설정이 없다. 메시지에는 설정 이름만 담는다."""


@dataclass(frozen=True)
class BlockResult:
    ok: bool
    status: str  # blocked / http_error / timeout / network_error
    http_status: int | None = None


class SupabaseAuthAdmin:
    def __init__(self, admin_url: str, service_role_key: str, *, ban_duration: str, timeout_seconds: float,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not admin_url:
            raise AuthAdminConfigError("Supabase Auth 관리자 주소가 없습니다.")
        if not service_role_key:
            raise AuthAdminConfigError("Supabase Auth service_role 키가 없습니다.")
        self.admin_url = admin_url.rstrip("/")
        self._key = service_role_key
        self.ban_duration = ban_duration
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def __repr__(self) -> str:  # 키를 표시하지 않는다.
        return f"SupabaseAuthAdmin(admin_url={self.admin_url!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupabaseAuthAdmin:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def block_new_logins(self, user_id: UUID) -> BlockResult:
        """사용자에게 ban_duration을 건다. 같은 요청을 다시 보내도 결과는 같다(차단 만료 시각만 다시 계산)."""
        url = f"{self.admin_url}/admin/users/{user_id}"
        headers = {"Authorization": f"Bearer {self._key}", "apikey": self._key}
        try:
            response = self._client.put(url, headers=headers, json={"ban_duration": self.ban_duration})
        except httpx.TimeoutException:
            logger.warning("Auth 관리자 차단 요청 시간 초과 (사용자 %s)", user_id)
            return BlockResult(False, "timeout")
        except httpx.HTTPError as exc:
            logger.warning("Auth 관리자 차단 요청 실패 (사용자 %s): %s", user_id, type(exc).__name__)
            return BlockResult(False, "network_error")
        if 200 <= response.status_code < 300:
            return BlockResult(True, "blocked", response.status_code)
        logger.warning("Auth 관리자 차단 응답 HTTP %s (사용자 %s)", response.status_code, user_id)
        return BlockResult(False, "http_error", response.status_code)


def load_auth_admin(settings: Settings, env_file: Path | None = None,
                    transport: httpx.BaseTransport | None = None) -> SupabaseAuthAdmin:
    """설정(.env.api) → 루트 .env 순으로 service_role 키를 찾는다. 주소·키가 없으면 AuthAdminConfigError."""
    key = settings.auth_service_role_key.get_secret_value() if settings.auth_service_role_key else ""
    if not key:
        env = {k: (v or "").strip() for k, v in dotenv_values(env_file or SUPABASE_ENV_FILE).items()}
        key = env.get("SERVICE_ROLE_KEY", "")
    missing = []
    if not settings.auth_admin_url:
        missing.append(".env.api BACKEND_AUTH_ADMIN_URL")
    if not key:
        missing.append(".env.api BACKEND_AUTH_SERVICE_ROLE_KEY 또는 .env SERVICE_ROLE_KEY")
    if missing:
        raise AuthAdminConfigError("Supabase Auth 관리자 설정이 없습니다: " + ", ".join(missing))
    return SupabaseAuthAdmin(settings.auth_admin_url, key, ban_duration=settings.auth_ban_duration,
                             timeout_seconds=settings.auth_admin_timeout_seconds, transport=transport)
