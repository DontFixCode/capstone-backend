import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException

from app.core.config import Settings
from app.core.exceptions import install_error_handlers
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.devtools.daejeon_router import router as daejeon_devtools_router
from app.devtools.router import router as devtools_router
from app.modules.journeys.router import router as journeys_router
from app.modules.routing.router import router as routing_router
from app.modules.transit.router import router as transit_router
from app.modules.users.router import router as users_router
from app.realtime.router import router as realtime_router

settings = Settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # devtools가 첫 확인 요청 때 만든 TAGO 클라이언트의 연결을 닫는다.
    if app.state.tago_client is not None:
        app.state.tago_client.close()


# Swagger 태그 순서 = 앱 시험 순서(회원정보 → 정적 교통정보 → 장소·경로 검색 → 이동 세션).
OPENAPI_TAGS = [
    {"name": "me", "description": "1. 회원정보: 첫 조회에서 프로필 초기화"},
    {"name": "static-data", "description": "2. 정적 교통정보 공개본 버전"},
    {"name": "stops", "description": "3. 정류소 조회·도착정보"},
    {"name": "routes", "description": "4. 노선·패턴·경유 순서·형상·차량 위치"},
    {"name": "places", "description": "5. 장소 검색(제공처 미설정 시 503)"},
    {"name": "route-searches", "description": "6. 경로 검색 접수(202) → 결과 조회"},
    {"name": "journeys", "description": "7. 이동 시작·복구·수동 탑승/하차·위치. 스트림은 WS /api/v1/journeys/{journeyId}/stream"},
]
app = FastAPI(lifespan=lifespan, openapi_tags=OPENAPI_TAGS)
app.state.settings = settings
app.state.tago_client = None
# 앱 API 공통 자원. 첫 요청 때 설정으로 만들며, 시험·별도 실행기는 미리 주입할 수 있다(app/core/dependencies.py).
app.state.db = None
app.state.token_verifier = None
app.state.places_provider = None
install_error_handlers(app)
app.include_router(users_router)
app.include_router(transit_router)
app.include_router(routing_router)
app.include_router(journeys_router)
app.include_router(realtime_router)
logger = logging.getLogger(__name__)

# 개발 환경에서 명시적으로 켠 경우에만 등록한다. 등록하지 않으면 URL은 404이고 OpenAPI에도 없다.
if settings.devtools_enabled:
    app.include_router(devtools_router)
    app.include_router(daejeon_devtools_router)
    logger.warning("개발용 확인 URL(/dev/transit, /dev/daejeon)이 활성화되었습니다. 요청마다 TAGO·대전시 API를 호출합니다.")


@app.get("/health")
def health() -> dict[str, str]:
    """외부 서비스에 연결하지 않고 프로세스 생존 상태를 반환한다."""
    return {"status": "ok"}


@app.get("/db-test")
def test_db_connection() -> dict[str, object]:
    """현재 대상 DB(app/db/session.py 설정, SSH 터널)에 연결해 SELECT 1을 실행한다. /health와 달리 DB에 접속한다."""
    try:
        db = Database.from_settings(app.state.settings)
    except DbConfigError as error:
        logger.error("DB connection settings are missing: %s", error)
        raise HTTPException(status_code=503, detail="DB connection settings are missing.") from None
    try:
        with db.transaction() as conn:
            result = conn.execute("SELECT 1").fetchone()[0]
    except (DbError, psycopg.Error) as error:
        logger.error("DB connection test failed: %s", db.redact(str(error)))
        raise HTTPException(status_code=503, detail="DB connection test failed.") from None
    return {"status": "ok", "result": result}
