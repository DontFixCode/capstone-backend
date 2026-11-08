"""장소 검색과 최초 경로 검색 HTTP API (/api/v1/places/search, /api/v1/route-searches).

검색 접수(POST)는 입력을 검증해 processing으로 저장하고 202를 반환한다. 계산(DFS·TMAP·공유 수집·평가·추천·저장)은
워커가 같은 행을 가져가 실행하므로 API 서버는 공공 교통 API·TMAP을 호출하지 않는다.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.core.config import Settings
from app.core.dependencies import active_user, get_db, get_settings, profile_user
from app.core.exceptions import ApiError, not_found
from app.core.security import AuthUser
from app.db.session import Database
from app.integrations import places_api
from app.modules.routing import repository as search_repository
from app.modules.routing import views
from app.workflows import search as search_flow

router = APIRouter(prefix="/api/v1")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 장소 검색

def places_provider(request: Request) -> places_api.PlacesProvider:
    provider = getattr(request.app.state, "places_provider", None)
    if provider is not None:
        return provider
    return places_api.provider_from_settings(request.app.state.settings)


@router.get("/places/search", tags=["places"])
def places_search(request: Request, q: str = Query(), _user: AuthUser = Depends(active_user)) -> dict:
    query = q.strip()
    if not query:
        raise ApiError(422, "VALIDATION_ERROR", "검색어가 비어 있습니다.")
    try:
        provider = places_provider(request)
        items = places_api.validate_items(provider.search(query))
    except places_api.PlacesError as exc:
        raise ApiError(exc.status_code, exc.code, exc.message) from None
    return {"items": [{"name": i.name, "address": i.address, "latitude": i.latitude, "longitude": i.longitude,
                       "provider": i.provider, "externalId": i.external_id} for i in items]}


# ---------------------------------------------------------------- 경로 검색

class PlaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    provider: str | None = Field(default=None, max_length=50)
    externalId: str | None = Field(default=None, max_length=200)

    @field_validator("latitude", "longitude")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("유한한 좌표여야 합니다.")
        return value

    def to_input(self) -> search_flow.PlaceInput:
        return search_flow.PlaceInput(self.latitude, self.longitude, name=self.name, address=self.address,
                                      provider=self.provider, external_id=self.externalId)


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: PlaceBody
    destination: PlaceBody
    startAt: AwareDatetime | None = None


@router.post("/route-searches", status_code=202, tags=["route-searches"])
def create_route_search(body: SearchBody, user: AuthUser = Depends(profile_user), db: Database = Depends(get_db),
                        settings: Settings = Depends(get_settings)) -> JSONResponse:
    try:
        config = search_flow.load_search_config(settings)
    except search_flow.SearchConfigError:
        raise ApiError(503, "SEARCH_NOT_CONFIGURED", "경로 검색 설정이 없어 검색을 접수할 수 없습니다.") from None
    request = search_flow.SearchRequest(body.origin.to_input(), body.destination.to_input(),
                                        start_at=body.startAt.astimezone(timezone.utc) if body.startAt else None,
                                        source=search_flow.SOURCE_APP)
    try:
        accepted = search_flow.accept_app_search(db, config, user.user_id, request, now=_now())
    except search_flow.StaticDataUnavailable:
        raise ApiError(503, "STATIC_DATA_UNAVAILABLE", "정적 교통정보 공개본이 없어 검색을 접수할 수 없습니다.") from None
    search_id = str(accepted["search_id"])
    return JSONResponse(status_code=202, headers={"Location": f"/api/v1/route-searches/{search_id}"},
                        content={"searchId": search_id, "status": accepted["status"],
                                 "expiresAt": accepted["expires_at"].isoformat()})


@router.get("/route-searches/{searchId}", tags=["route-searches"])
def get_route_search(searchId: UUID, user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction(isolation="REPEATABLE READ") as conn:
        search = search_repository.read_user_search(conn, searchId, user.user_id)
        if search is None:
            raise not_found()
        candidates = [] if search["status"] == "processing" else search_repository.read_candidates(conn, searchId)
    return views.search_view(search, candidates, _now())
