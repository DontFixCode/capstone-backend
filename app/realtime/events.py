"""WebSocket으로 보내는 이벤트 형식. 추천·확률을 계산하지 않고 DB에 저장된 최신 상태만 옮긴다.

- journey_state: 연결 직후·상태/결과 버전이 바뀔 때 보내는 전체 상태. stateVersion(현재 이동 상태),
  baseStateVersion(안내 계산에 쓴 상태), resultVersion(안내 결과)을 함께 보내 앱이 맞는 결과만 적용하게 한다.
- journey_ended: 완료·취소 뒤 마지막 상태를 보내고 연결을 닫는다.
하차 임박·환승 알림의 조건·payload는 프론트엔드 사양 확인 후 추가한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 연결 종료 코드(4000번대는 애플리케이션 정의). 이유 문자열에 토큰을 넣지 않는다.
CLOSE_UNAUTHORIZED = 4401
CLOSE_ACCOUNT_DELETED = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_SERVER_UNAVAILABLE = 1011
CLOSE_NORMAL = 1000


def state_message(view: dict[str, Any], *, sent_at: datetime, kind: str = "journey_state") -> dict[str, Any]:
    guidance = view.get("guidance")
    return {
        "type": kind,
        "journeyId": view["journeyId"],
        "stateVersion": view["stateVersion"],
        "baseStateVersion": guidance.get("baseStateVersion") if guidance else None,
        "resultVersion": view["resultVersion"],
        "selectionVersion": view["selectionVersion"],
        "lifecycleStatus": view["lifecycleStatus"],
        "movementState": view["movementState"],
        "guidance": guidance,
        "guidanceStatus": view["guidanceStatus"],
        "proposedRoute": view.get("proposedRoute"),
        "currentProgress": view["currentProgress"],
        "sentAt": sent_at.isoformat(),
    }
