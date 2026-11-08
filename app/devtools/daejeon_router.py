"""대전광역시 버스 API 확인용 개발 라우터(Notion Backend > 검증 대기 항목 2번).

개발 환경에서 ENABLE_DEVTOOLS=true일 때만 main.py가 등록한다. 요청 한 번에 대전시 API를 1회 호출하고(재시도 없음)
제공기관의 응답 본문·HTTP 상태·Content-Type을 그대로 돌려준다. 본문에 인증키가 되돌아오면 그 부분만 가린다.
요청·수신 시각(UTC)은 본문을 바꾸지 않도록 응답 헤더로만 붙인다. 응답을 받지 못한 경우(시간초과·연결 오류)만 JSON 오류다.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response

from app.integrations.daejeon_bus_api import DaejeonBusClient, DaejeonBusConfigError

router = APIRouter(prefix="/dev/daejeon", tags=["devtools: 대전시 버스 확인"])

_STATUS_BY_CATEGORY = {"timeout": 504}

BusStopId = Annotated[str, Path(min_length=1, max_length=30, pattern=r"^\S+$",
                                description="대전시 정류소 ID BusStopID (TAGO nodeId와 대응 미확인)")]


@router.get("/stops/{bus_stop_id}/arrivals", summary="대전시 정류소 도착정보 원본 (getArrInfoByStopID)",
            response_class=Response)
def stop_arrivals(request: Request, bus_stop_id: BusStopId) -> Response:
    state = request.app.state
    try:
        # 시험은 app.state.daejeon_bus_transport로 가짜 전송을 주입한다.
        client = DaejeonBusClient.from_settings(state.settings,
                                                transport=getattr(state, "daejeon_bus_transport", None))
    except DaejeonBusConfigError as error:
        raise HTTPException(status_code=503, detail=f"서버 설정 오류: {error}") from None
    with client:
        result = client.arrivals_by_stop(bus_stop_id)
    headers = {"X-Upstream-Requested-At": result.requested_at}
    if result.error is not None:
        return JSONResponse({"error": result.error}, status_code=_STATUS_BY_CATEGORY.get(result.error["category"], 502),
                            headers=headers)
    headers["X-Upstream-Received-At"] = result.received_at or ""
    # 원래 Content-Type(문자 인코딩 포함)을 그대로 쓰고 본문 바이트를 다시 인코딩하지 않는다.
    if result.content_type:
        headers["Content-Type"] = result.content_type
    return Response(content=result.body or b"", status_code=result.http_status or 502, headers=headers)
