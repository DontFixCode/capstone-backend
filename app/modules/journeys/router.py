"""이동 세션 HTTP API (/api/v1/journeys). 기록 삭제(DELETE)·환승 결과 입력은 Phase 6, 대안 수락·거절은 Phase 5 범위다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.core.config import Settings
from app.core.dependencies import active_user, get_db, get_settings, profile_user
from app.core.exceptions import ApiError
from app.core.security import AuthUser
from app.db.session import Database
from app.modules.journeys import service
from app.modules.journeys.schemas import EventBody, LocationBody, StartJourneyBody
from app.workflows.search import SearchConfigError, load_search_config

router = APIRouter(prefix="/api/v1", tags=["journeys"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _call(fn):
    try:
        return fn()
    except service.JourneyError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message, details=exc.details) from None


@router.post("/journeys", status_code=201)
def start_journey(body: StartJourneyBody, user: AuthUser = Depends(profile_user),
                  db: Database = Depends(get_db)) -> dict:
    now = _now()
    journey = _call(lambda: service.start_journey(db, user.user_id, body.searchId, body.candidateId, now=now))
    return service.journey_view(journey, now)


@router.get("/journeys")
def list_journeys(status: Literal["active", "completed", "cancelled"] | None = Query(default=None),
                  cursor: str | None = Query(default=None, max_length=500), user: AuthUser = Depends(active_user),
                  db: Database = Depends(get_db)) -> dict:
    rows, next_cursor = _call(lambda: service.list_user_journeys(db, user.user_id, status=status, cursor=cursor))
    return {"items": [service.journey_summary(r) for r in rows], "nextCursor": next_cursor}


@router.get("/journeys/{journeyId}")
def get_journey(journeyId: UUID, user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    journey = _call(lambda: service.get_user_journey(db, user.user_id, journeyId))
    return service.journey_view(journey, _now())


@router.post("/journeys/{journeyId}/events")
def post_event(journeyId: UUID, body: EventBody,
               idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
               user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    key = idempotency_key.strip()
    if not key:
        raise ApiError(422, "VALIDATION_ERROR", "Idempotency-Key가 비어 있습니다.")
    result = _call(lambda: service.record_event(db, user.user_id, journeyId, key, body, now=_now()))
    return {"eventId": str(result["event_id"]), "stateVersionAfter": result["state_version_after"],
            "replayed": result["replayed"]}


@router.put("/journeys/{journeyId}/location", status_code=204)
def put_location(journeyId: UUID, body: LocationBody, user: AuthUser = Depends(active_user),
                 db: Database = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    try:
        max_age = settings.journey_location_max_age_seconds or load_search_config(settings).evaluation_policy \
            .max_data_age_seconds
    except SearchConfigError:
        raise ApiError(503, "SEARCH_NOT_CONFIGURED", "위치 최신성 기준 설정이 없습니다.") from None
    _call(lambda: service.update_location(db, user.user_id, journeyId, body, now=_now(), settings=settings,
                                          max_age_seconds=max_age))
    return Response(status_code=204)
