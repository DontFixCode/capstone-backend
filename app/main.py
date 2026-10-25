import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException

from app.core.config import Settings
from app.core.logging import configure_logging
from app.devtools.router import router as devtools_router
from app.modules.journeys.router import router as journeys_router
from app.modules.routing.router import router as routing_router
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


app = FastAPI(lifespan=lifespan)
app.state.settings = settings
app.state.tago_client = None
app.include_router(users_router)
app.include_router(journeys_router)
app.include_router(routing_router)
app.include_router(realtime_router)
logger = logging.getLogger(__name__)
config = dotenv_values(Path(__file__).resolve().parents[1] / ".env")

# 개발 환경에서 명시적으로 켠 경우에만 등록한다. 등록하지 않으면 URL은 404이고 OpenAPI에도 없다.
if settings.devtools_enabled:
    app.include_router(devtools_router)
    logger.warning("개발용 확인 URL(/dev/transit)이 활성화되었습니다. 요청마다 TAGO API를 호출합니다.")


@app.get("/health")
def health() -> dict[str, str]:
    """외부 서비스에 연결하지 않고 프로세스 생존 상태를 반환한다."""
    return {"status": "ok"}


@app.get("/db-test")
def test_db_connection():
    required_keys = (
        "POSTGRES_HOST",
        "POSTGRES_HOST_PORT",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POOLER_TENANT_ID",
    )
    if not all(config.get(key) for key in required_keys):
        raise HTTPException(status_code=503, detail="DB connection settings are missing in .env.")

    try:
        with psycopg.connect(
            host=config["POSTGRES_HOST"],
            port=config["POSTGRES_HOST_PORT"],
            dbname=config["POSTGRES_DB"],
            user=f"postgres.{config['POOLER_TENANT_ID']}",
            password=config["POSTGRES_PASSWORD"],
            connect_timeout=5,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
    except psycopg.Error as error:
        # print(error.__traceback__())
        logger.error(
            "DB connection test failed: %s",
            str(error).replace(config["POSTGRES_PASSWORD"], "<redacted>"),
        )
        print("detected")
        raise HTTPException(status_code=503, detail="DB connection test failed.") from None

    return {"status": "ok", "result": result[0]}
