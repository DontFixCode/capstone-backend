"""Phase 1 교통 데이터 확인용 개발 라우터.

개발 환경에서 ENABLE_DEVTOOLS=true일 때만 main.py가 등록한다. 요청 한 번에 지정한 대상의 한 페이지만
TAGO에 요청하며(재시도 없음), 메타데이터와 인증키를 가린 원본 응답을 나눠 반환한다.
"""

import threading
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from app.integrations import bus_api
from app.integrations.bus_api import Endpoint, Page, TagoClient, TagoConfigError

router = APIRouter(prefix="/dev/transit", tags=["devtools: TAGO 확인"])

CITY_CODE = bus_api.DAEJEON_CITY_CODE
# 확인용 URL은 재시도하지 않는다. 요청당 외부 HTTP 시도는 1회다.
MAX_RETRIES = 0
OBSERVED_AT_NOTE = "received_at은 이 서버가 응답을 받은 시각(UTC)이며 원천 관측 시각이 아니다. 응답 명세에 관측 시각 항목이 없다."
# 시간초과·호출 한도 외의 외부 오류는 502로 반환한다.
_STATUS_BY_CATEGORY = {"timeout": 504, "rate_limit": 429}
# 공용 클라이언트를 순차 사용해 요청 간격 설정이 요청 사이에 적용되게 한다.
_lock = threading.Lock()

PageNo = Annotated[int, Query(ge=1, description="페이지 번호 pageNo")]
Rows = Annotated[int, Query(ge=1, le=100, description="페이지 크기 numOfRows (확인용 URL 최대 100)")]
# 노선 하나의 경유 정류소를 한 번에 보도록 기본값과 상한을 더 크게 둔다.
StopRows = Annotated[int, Query(ge=1, le=500, description="페이지 크기 numOfRows (경유 정류소 최대 500)")]
RouteId = Annotated[str, Path(min_length=1, max_length=30, pattern=r"^\S+$", description="노선 ID routeId")]
StopId = Annotated[str, Path(min_length=1, max_length=30, pattern=r"^\S+$", description="정류소 ID nodeId")]


def _optional_text(max_length: int, description: str) -> Any:
    return Query(min_length=1, max_length=max_length, pattern=r"\S", description=description)


@router.get("/stops", summary="정류소 목록 (getSttnNoList)")
def stops(
    request: Request,
    stop_name: Annotated[str | None, _optional_text(30, "정류소명 검색 nodeNm")] = None,
    stop_no: Annotated[str | None, _optional_text(10, "정류소번호 nodeNo")] = None,
    page_no: PageNo = 1,
    num_of_rows: Rows = 10,
) -> JSONResponse:
    params = {"cityCode": CITY_CODE, "nodeNm": stop_name, "nodeNo": stop_no}
    return _fetch(request, bus_api.STATION_LIST, params, page_no, num_of_rows)


@router.get("/stops/nearby", summary="좌표 주변 정류소 (getCrdntPrxmtSttnList)")
def nearby_stops(
    request: Request,
    lat: Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False, description="WGS84 위도 gpsLati")],
    lon: Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False, description="WGS84 경도 gpsLong")],
    page_no: PageNo = 1,
    num_of_rows: Rows = 10,
) -> JSONResponse:
    return _fetch(request, bus_api.NEARBY_STATIONS, {"gpsLati": lat, "gpsLong": lon}, page_no, num_of_rows)


@router.get("/routes", summary="노선 목록 (getRouteNoList)")
def routes(
    request: Request,
    route_no: Annotated[str | None, _optional_text(30, "노선번호 routeNo")] = None,
    page_no: PageNo = 1,
    num_of_rows: Rows = 10,
) -> JSONResponse:
    params = {"cityCode": CITY_CODE, "routeNo": route_no}
    return _fetch(request, bus_api.ROUTE_LIST, params, page_no, num_of_rows)


@router.get("/routes/{route_id}", summary="노선 기본정보 (getRouteInfoIem, 페이지 없음)")
def route_info(request: Request, route_id: RouteId) -> JSONResponse:
    return _fetch(request, bus_api.ROUTE_INFO, {"cityCode": CITY_CODE, "routeId": route_id})


@router.get("/routes/{route_id}/stops", summary="노선별 경유 정류소 (getRouteAcctoThrghSttnList)")
def route_stops(request: Request, route_id: RouteId, page_no: PageNo = 1, num_of_rows: StopRows = 200) -> JSONResponse:
    params = {"cityCode": CITY_CODE, "routeId": route_id}
    return _fetch(request, bus_api.ROUTE_STOPS, params, page_no, num_of_rows)


@router.get("/routes/{route_id}/positions", summary="노선별 버스 위치 (getRouteAcctoBusLcList)")
def route_positions(request: Request, route_id: RouteId, page_no: PageNo = 1, num_of_rows: Rows = 10) -> JSONResponse:
    params = {"cityCode": CITY_CODE, "routeId": route_id}
    return _fetch(request, bus_api.BUS_LOCATIONS, params, page_no, num_of_rows)


@router.get("/stops/{stop_id}/arrivals", summary="정류소 도착예정정보 (getSttnAcctoArvlPrearngeInfoList)")
def stop_arrivals(request: Request, stop_id: StopId, page_no: PageNo = 1, num_of_rows: Rows = 10) -> JSONResponse:
    params = {"cityCode": CITY_CODE, "nodeId": stop_id}
    return _fetch(request, bus_api.ARRIVALS, params, page_no, num_of_rows)


def _client(request: Request) -> TagoClient:
    """첫 확인 요청 때만 인증키를 검사해 공용 클라이언트를 만든다. 앱 종료 시 main.py가 닫는다."""
    state = request.app.state
    with _lock:
        if getattr(state, "tago_client", None) is None:
            try:
                state.tago_client = TagoClient.from_settings(state.settings, max_retries=MAX_RETRIES)
            except TagoConfigError as error:
                raise HTTPException(status_code=503, detail=f"서버 설정 오류: {error}") from None
        return state.tago_client


def _fetch(request: Request, endpoint: Endpoint, params: dict[str, Any], page_no: int = 1, rows: int = 10) -> JSONResponse:
    client = _client(request)
    with _lock:
        page = client.fetch_page(endpoint, params, page_no=page_no, rows=rows)
    content: dict[str, Any] = {"meta": _meta(endpoint, page)}
    status_code = 200
    if page.error is not None:
        error = page.error
        content["error"] = {"category": error.category, "source": error.source, "code": error.code, "message": error.message}
        status_code = _STATUS_BY_CATEGORY.get(error.category, 502)
    content["raw"] = page.raw
    return JSONResponse(content, status_code=status_code)


def _meta(endpoint: Endpoint, page: Page) -> dict[str, Any]:
    """요청 맥락과 응답 수신 정보. 원본 응답(raw)과 섞지 않는다."""
    last = page.attempts[-1] if page.attempts else None
    header = page.header or {}
    return {
        "tago_endpoint": f"/1613000/{endpoint.path}",
        "params": {**page.params, "_type": "json"},
        "requested_at": page.requested_at,
        "received_at": page.received_at,
        "http_attempts": len(page.attempts),
        "http_status": last.http_status if last else None,
        "result_code": header.get("resultCode"),
        "result_msg": header.get("resultMsg"),
        "paging": page.meta,
        "item_count": len(page.items),
        "status": page.status,
        "source_observed_at": None,
        "note": OBSERVED_AT_NOTE,
    }
