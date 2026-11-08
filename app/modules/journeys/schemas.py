"""이동 세션 요청 형식(journeys API 명세의 camelCase)."""

from __future__ import annotations

import math
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

EVENT_TYPES = ("board", "alight", "missed_bus", "destination_changed", "route_accepted", "route_rejected",
               "transfer_result", "completed", "cancelled", "correction")


class StartJourneyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    searchId: UUID
    candidateId: UUID


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eventType: Literal["board", "alight", "missed_bus", "destination_changed", "route_accepted", "route_rejected",
                       "transfer_result", "completed", "cancelled", "correction"]
    stateVersion: int = Field(ge=1)
    occurredAt: AwareDatetime | None = None
    payload: dict[str, Any]


class BoardPayload(BaseModel):
    """확인한 노선·방향·차량. 차량을 모르면 vehicleId는 null이며 계획 차량으로 채우지 않는다."""

    model_config = ConfigDict(extra="forbid")
    legIndex: int = Field(ge=0)
    routeId: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    patternKey: str | None = Field(default=None, min_length=1, max_length=200)
    directionCode: str | None = Field(default=None, max_length=20)
    vehicleId: str | None = Field(default=None, max_length=50)
    confirmation: Literal["user_confirmed", "user_selected_route", "user_entered_vehicle"] | None = None


class AlightPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    legIndex: int = Field(ge=0)
    stopId: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    seq: int | None = Field(default=None, ge=1)


class CompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=200)


class CancelledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=200)


class CorrectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supersedesEventId: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    routeId: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    patternKey: str | None = Field(default=None, min_length=1, max_length=200)
    directionCode: str | None = Field(default=None, max_length=20)
    vehicleId: str | None = Field(default=None, max_length=50)
    stopId: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    seq: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=200)


PAYLOADS: dict[str, type[BaseModel]] = {"board": BoardPayload, "alight": AlightPayload,
                                         "completed": CompletedPayload, "cancelled": CancelledPayload,
                                         "correction": CorrectionPayload}


class LocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observedAt: AwareDatetime
    accuracyM: float | None = Field(default=None, ge=0)

    @field_validator("latitude", "longitude", "accuracyM")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("유한한 값이어야 합니다.")
        return value
