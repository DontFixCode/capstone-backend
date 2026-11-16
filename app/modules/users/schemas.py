"""회원정보 요청·응답 형식. 응답 필드는 API 명세(me)의 camelCase를 쓴다."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProfileResponse(BaseModel):
    userId: UUID
    displayName: str | None
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ProfileResponse:
        return cls(userId=row["user_id"], displayName=row["display_name"], createdAt=row["created_at"],
                   updatedAt=row["updated_at"])


class AccountDeletionResponse(BaseModel):
    """탈퇴 접수(202). 표시용 ID만 _deleted를 붙이고 실제 UUID는 바꾸지 않는다.

    authBlockStatus: blocked(Auth 신규 로그인 차단 확인) / pending(차단 실패·미설정, 서버가 재시도). 어느 쪽이든
    이 서버의 개인 API·WebSocket 접근은 이미 차단됐다.
    """

    displayUserId: str
    deletedAt: datetime
    authBlockStatus: Literal["blocked", "pending"]
    authBlockedAt: datetime | None
    cancelledJourneyIds: list[str]

    @classmethod
    def from_outcome(cls, outcome: Any) -> AccountDeletionResponse:
        from app.modules.users.service import display_user_id

        profile = outcome.profile
        return cls(displayUserId=display_user_id(profile), deletedAt=profile["deleted_at"],
                   authBlockStatus="blocked" if profile["auth_blocked_at"] is not None else "pending",
                   authBlockedAt=profile["auth_blocked_at"],
                   cancelledJourneyIds=[str(j) for j in outcome.cancelled_journey_ids])


class ProfilePatch(BaseModel):
    """displayName: 문자열이면 변경, null이면 지움, 생략하면 유지. 알 수 없는 필드는 거부한다."""

    model_config = ConfigDict(extra="forbid")
    displayName: str | None = Field(default=None)

    def provided(self) -> bool:
        return "displayName" in self.model_fields_set
