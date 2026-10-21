import logging
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException

from app.core.config import Settings
from app.core.logging import configure_logging
from app.modules.journeys.router import router as journeys_router
from app.modules.routing.router import router as routing_router
from app.modules.users.router import router as users_router
from app.realtime.router import router as realtime_router

settings = Settings()
configure_logging(settings.log_level)

app = FastAPI()
app.include_router(users_router)
app.include_router(journeys_router)
app.include_router(routing_router)
app.include_router(realtime_router)
logger = logging.getLogger(__name__)
config = dotenv_values(Path(__file__).resolve().parents[1] / ".env")


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
