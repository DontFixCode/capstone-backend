"""WS /api/v1/journeys/{journeyId}/stream — 인증된 소유자에게 최신 안내를 전달하고 재접속 때 현재 상태를 동기화한다.

인증(앱 계약이 없어 권장안 적용):
1. 네이티브 앱의 Authorization: Bearer 헤더(기본). 헤더가 있으면 연결 승인 전에 검증한다.
2. 헤더를 보낼 수 없는 환경이면 연결 후 ws_auth_timeout_seconds 안에 첫 메시지 {"type": "auth", "accessToken": "..."}.
토큰을 URL 쿼리로 받지 않고 로그에 남기지 않는다. 인증·소유권·탈퇴 확인 전에는 세션 데이터를 보내지 않는다.

연결 후: 저장된 최신 상태(journey_state)를 먼저 보내고, ws_poll_seconds마다 DB의 state/result 버전을 확인해 바뀌면 다시 보낸다.
앱 메시지 {"type": "sync"}는 즉시 현재 상태를, {"type": "ping"}은 pong을 돌려준다. 연결이 끊겨도 이동·재평가는 계속된다.
완료·취소되면 journey_ended를 보내고 닫는다. 탈퇴 처리되면 4403으로 닫는다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.core.dependencies import authenticate, check_account, database_for, verifier_for
from app.core.exceptions import ApiError
from app.core.security import AuthError, AuthUser
from app.realtime import events, publisher
from app.realtime.connections import registry

router = APIRouter()
logger = logging.getLogger(__name__)


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        pass


async def _authenticate(websocket: WebSocket, verifier, timeout: float) -> AuthUser | None:
    header = websocket.headers.get("authorization")
    if header:
        try:
            user = authenticate(verifier, header)
        except ApiError as exc:
            await _close(websocket, events.CLOSE_UNAUTHORIZED, exc.code)
            return None
        await websocket.accept()
        return user
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        await _close(websocket, events.CLOSE_UNAUTHORIZED, "AUTH_REQUIRED")
        return None
    token = message.get("accessToken") if isinstance(message, dict) and message.get("type") == "auth" else None
    if not isinstance(token, str):
        await _close(websocket, events.CLOSE_UNAUTHORIZED, "AUTH_REQUIRED")
        return None
    try:
        return verifier.verify(token)
    except AuthError as exc:
        await _close(websocket, events.CLOSE_UNAUTHORIZED, exc.code)
        return None


@router.websocket("/api/v1/journeys/{journeyId}/stream")
async def journey_stream(websocket: WebSocket, journeyId: UUID) -> None:
    app = websocket.app
    settings = app.state.settings
    try:
        verifier = verifier_for(app)
        db = database_for(app)
    except ApiError:
        await _close(websocket, events.CLOSE_SERVER_UNAVAILABLE, "SERVER_UNAVAILABLE")
        return
    user = await _authenticate(websocket, verifier, settings.ws_auth_timeout_seconds)
    if user is None:
        return
    try:
        await run_in_threadpool(check_account, db, user, create_profile=False)
    except ApiError as exc:
        code = events.CLOSE_ACCOUNT_DELETED if exc.code == "ACCOUNT_DELETED" else events.CLOSE_UNAUTHORIZED
        await _close(websocket, code, exc.code)
        return
    message = await run_in_threadpool(publisher.load_state, db, journeyId, user.user_id)
    if message is None:
        await _close(websocket, events.CLOSE_NOT_FOUND, "NOT_FOUND")
        return

    registry.add(journeyId)
    await run_in_threadpool(publisher.mark_connection, db, journeyId, "connected")
    seen = publisher.Seen(message["stateVersion"], message["resultVersion"])
    close = (events.CLOSE_NORMAL, "JOURNEY_ENDED")
    try:
        await websocket.send_json(message)
        if message["type"] == "journey_ended":
            return
        while True:
            if datetime.now(timezone.utc) >= user.expires_at:
                close = (events.CLOSE_UNAUTHORIZED, "TOKEN_EXPIRED")
                return
            try:
                incoming = await asyncio.wait_for(websocket.receive_json(), timeout=settings.ws_poll_seconds)
            except asyncio.TimeoutError:
                incoming = None
            except ValueError:
                incoming = {"type": "invalid"}
            if isinstance(incoming, dict) and incoming.get("type") == "ping":
                await websocket.send_json({"type": "pong", "sentAt": datetime.now(timezone.utc).isoformat()})
                continue
            force = isinstance(incoming, dict) and incoming.get("type") == "sync"
            versions = await run_in_threadpool(publisher.poll_versions, db, journeyId)
            if versions is None:
                close = (events.CLOSE_NOT_FOUND, "NOT_FOUND")
                return
            if versions["deleted_at"] is not None:
                close = (events.CLOSE_ACCOUNT_DELETED, "ACCOUNT_DELETED")
                return
            changed = (versions["state_version"], versions["result_version"]) != (seen.state_version,
                                                                                   seen.result_version)
            ended = versions["lifecycle_status"] != "active"
            if changed or force or ended:
                message = await run_in_threadpool(publisher.load_state, db, journeyId, user.user_id)
                if message is None:
                    close = (events.CLOSE_NOT_FOUND, "NOT_FOUND")
                    return
                await websocket.send_json(message)
                seen = publisher.Seen(message["stateVersion"], message["resultVersion"])
                if message["type"] == "journey_ended":
                    return
    except WebSocketDisconnect:
        close = None
    finally:
        if registry.remove(journeyId) == 0:
            try:
                await run_in_threadpool(publisher.mark_connection, db, journeyId, "disconnected")
            except Exception:  # 연결 상태 기록 실패는 이동에 영향을 주지 않는다.
                logger.warning("이동 %s 연결 상태 기록 실패", journeyId)
        if close is not None:
            await _close(websocket, *close)
