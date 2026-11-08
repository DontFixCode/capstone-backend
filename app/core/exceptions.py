"""공통 예외와 일관된 오류 응답.

앱 API 오류는 {"error": {"code": ..., "message": ...}} 형식으로 반환한다. 401에는 WWW-Authenticate를 붙인다.
DB 오류 메시지는 가린 뒤 로그에만 남기고 응답에는 코드만 준다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db.session import DbError

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, details: Any = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


def unauthorized(code: str, message: str) -> ApiError:
    return ApiError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


def not_found(message: str = "리소스가 없거나 요청 사용자 소유가 아닙니다.") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def conflict(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(409, code, message, details=details)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(exc.body()), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e.get("loc", ())), "type": e.get("type"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "요청 형식이나 값이 잘못됐습니다.", "details": details}})

    @app.exception_handler(DbError)
    async def _db_error(_request: Request, exc: DbError) -> JSONResponse:
        logger.error("DB 오류: %s", exc)
        return JSONResponse(status_code=503, content={"error": {
            "code": "DB_UNAVAILABLE", "message": "DB를 사용할 수 없습니다."}})
