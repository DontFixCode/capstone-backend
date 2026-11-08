"""회원정보 요청·응답 형식. 응답 필드는 API 명세(me)의 camelCase를 쓴다."""

from __future__ import annotations

from datetime import datetime
from typing import Any
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


class ProfilePatch(BaseModel):
    """displayName: 문자열이면 변경, null이면 지움, 생략하면 유지. 알 수 없는 필드는 거부한다."""

    model_config = ConfigDict(extra="forbid")
    displayName: str | None = Field(default=None)

    def provided(self) -> bool:
        return "displayName" in self.model_fields_set
