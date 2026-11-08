"""대전광역시 버스 도착정보 API(공공데이터포털 apis.data.go.kr/6300000/arrive) 확인용 클라이언트.

Phase 3 - Algorithms 1.2.2 ①이 활용 후보로 둔 대전시 API의 실제 응답을 확인하기 위한 최소 구현이다. 수집·평가에는 아직
연결하지 않는다. 응답 형식·결과 코드·필드 의미가 확인되지 않았으므로 본문을 해석하지 않고 그대로 돌려준다.

인증키는 요청 직전에만 쓰고, 응답 본문·오류 메시지에 키가 되돌아오면 그 부분만 가린다. httpx 요청 로그에는 키가 담긴
URL이 남으므로 경고 이상만 출력한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

BASE_URL = "https://apis.data.go.kr/6300000/arrive"
ARRIVALS_BY_STOP = "getArrInfoByStopID"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_URL_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}")
_SERVICE_KEY_PARAM = re.compile(r"(serviceKey=)[^&\s\"'<>]*", re.IGNORECASE)
_SERVICE_KEY_PARAM_BYTES = re.compile(rb"(serviceKey=)[^&\s\"'<>]*", re.IGNORECASE)


class DaejeonBusConfigError(RuntimeError):
    """인증키가 없거나 형식이 잘못됐다. 메시지에 키 값을 넣지 않는다."""


@dataclass
class RawResponse:
    """제공기관 응답 그대로(키만 가림). 응답을 받지 못했으면 error에 이유가 있고 body는 None이다."""

    requested_at: str
    received_at: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body: bytes | None = None
    error: dict[str, str] | None = None


class DaejeonBusClient:
    def __init__(self, service_key: str, *, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        key = (service_key or "").strip()
        if not key:
            raise DaejeonBusConfigError("대전시 버스 API 인증키가 비어 있습니다.")
        if _URL_ENCODED.search(key):
            raise DaejeonBusConfigError(
                "인증키에 URL 인코딩된 값(%XX)이 있습니다. 공공데이터포털의 일반 인증키(Decoding)를 입력하세요.")
        self._service_key = key
        variants = {key, quote(key, safe=""), quote_plus(key), str(httpx.QueryParams({"k": key}))[2:]}
        ordered = sorted(variants, key=len, reverse=True)
        self._key_pattern = re.compile("|".join(re.escape(v) for v in ordered), re.IGNORECASE)
        # 본문은 바이트 그대로 돌려주므로(문자 인코딩 보존) 키 가림도 바이트에서 한다.
        self._key_pattern_bytes = re.compile(b"|".join(re.escape(v.encode("utf-8")) for v in ordered), re.IGNORECASE)
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> DaejeonBusClient:
        secret = settings.daejeon_bus_service_key or settings.tago_service_key
        if secret is None:
            raise DaejeonBusConfigError(
                "DAEJEON_BUS_SERVICE_KEY(또는 TAGO_SERVICE_KEY)가 설정되지 않았습니다. .env.api에 공공데이터포털 "
                "일반 인증키(Decoding)를 입력하세요.")
        return cls(secret.get_secret_value(), **{"timeout": settings.daejeon_bus_timeout_seconds, **options})

    def __enter__(self) -> DaejeonBusClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return self._key_pattern.sub("***", _SERVICE_KEY_PARAM.sub(r"\1***", text))

    def redact_bytes(self, body: bytes) -> bytes:
        return self._key_pattern_bytes.sub(b"***", _SERVICE_KEY_PARAM_BYTES.sub(rb"\1***", body))

    def arrivals_by_stop(self, bus_stop_id: str) -> RawResponse:
        """정류소별 도착정보(getArrInfoByStopID). 요청 1회, 재시도 없음."""
        return self._get(ARRIVALS_BY_STOP, {"BusStopID": bus_stop_id})

    def _get(self, operation: str, params: dict[str, str]) -> RawResponse:
        result = RawResponse(requested_at=_utc_now())
        try:
            response = self._http.get(f"/{operation}", params={"serviceKey": self._service_key, **params})
        except httpx.TimeoutException as exc:
            result.error = {"category": "timeout", "message": f"응답 제한시간 초과 ({type(exc).__name__})"}
            return result
        except httpx.TransportError as exc:
            result.error = {"category": "network",
                            "message": f"연결 오류 ({type(exc).__name__}): {self.redact(str(exc))}"}
            return result
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            result.error = {"category": "http", "message": f"요청 처리 오류 ({type(exc).__name__})"}
            return result
        result.received_at = _utc_now()
        result.http_status = response.status_code
        result.content_type = response.headers.get("content-type")
        result.body = self.redact_bytes(response.content)
        return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
