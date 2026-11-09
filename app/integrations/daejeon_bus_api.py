"""대전광역시 버스 도착정보 API(공공데이터포털 apis.data.go.kr/6300000/arrive) 클라이언트.

실시간 도착정보의 수집 원천이다(2026-11-09 사용자 결정으로 TAGO 도착정보를 대체, 위치·정적 정보는 TAGO 유지).
- arrivals_by_stop: 응답 원본 그대로(devtools 확인 URL용, 키만 가림).
- fetch_arrivals: 수집용 해석. 확인된 응답 형식(ServiceResult > msgHeader·msgBody > itemList, 2026-11-09 실응답)을 읽는다.
  headerCd "0"만 정상으로 보고, 그 밖의 headerCd·공공데이터포털 게이트웨이 오류(returnReasonCode)·HTTP 오류는 실패다.
  운행 없음일 때의 결과 코드는 아직 확인하지 못했다(Notion Backend > 검증 대기 항목). itemPageCnt가 currentPage보다 크면
  전체 페이지를 받지 못한 것이므로 partial이다.

인증키는 요청 직전에만 쓰고, 응답 본문·오류 메시지에 키가 되돌아오면 그 부분만 가린다. httpx 요청 로그에는 키가 담긴
URL이 남으므로 경고 이상만 출력한다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
class ArrivalsPage:
    """수집용 해석 결과 1건(요청 1회). status: success / empty / partial / error / timeout."""

    params: dict[str, str]
    requested_at: str
    received_at: str | None
    http_status: int | None
    status: str
    provider_code: str | None = None
    header: dict[str, str] = field(default_factory=dict)
    items: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    body: str | None = None

    @property
    def item_count(self) -> int | None:
        text = self.header.get("itemCnt")
        return int(text) if text and text.strip().isdigit() else None

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
        """정류소별 도착정보(getArrInfoByStopID) 원본. 요청 1회, 재시도 없음."""
        return self._get(ARRIVALS_BY_STOP, {"BusStopID": bus_stop_id})

    def fetch_arrivals(self, bus_stop_id: str) -> ArrivalsPage:
        """정류소별 도착정보를 수집용으로 해석한다. 요청 1회, 재시도 없음(다음 수집 주기에 다시 조회)."""
        params = {"BusStopID": bus_stop_id}
        return parse_arrivals(self._get(ARRIVALS_BY_STOP, params), params)

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


_HEADER_TAGS = ("headerCd", "headerMsg", "itemCnt", "currentPage", "itemPageCnt", "returnReasonCode", "returnAuthMsg",
                "errMsg")


def parse_arrivals(raw: RawResponse, params: dict[str, str]) -> ArrivalsPage:
    page = ArrivalsPage(params=dict(params), requested_at=raw.requested_at, received_at=raw.received_at,
                        http_status=raw.http_status, status="error")
    if raw.error is not None:
        page.status = "timeout" if raw.error["category"] == "timeout" else "error"
        page.error = f"{raw.error['category']}: {raw.error['message']}"
        return page
    text = _decode(raw.body or b"", raw.content_type)
    page.body = text or None
    try:
        root = ET.fromstring(text.strip()) if text.strip().startswith("<") else None
    except ET.ParseError as exc:
        root = None
        page.error = f"parse: XML로 해석할 수 없는 응답 ({exc})"
    if root is not None:
        for element in root.iter():
            name = _local(element.tag)
            if name in _HEADER_TAGS and name not in page.header and len(element) == 0:
                page.header[name] = (element.text or "").strip()
    gateway = page.header.get("returnReasonCode")
    if gateway and gateway not in ("00", "0"):
        page.provider_code = gateway
        page.error = f"gateway: {gateway} {page.header.get('returnAuthMsg') or page.header.get('errMsg') or ''}".strip()
        return page
    if raw.http_status is None or not 200 <= raw.http_status < 300:
        page.error = f"http: HTTP {raw.http_status}"
        return page
    if root is None:
        page.error = page.error or "parse: XML 응답이 아님"
        return page
    page.provider_code = page.header.get("headerCd")
    if page.provider_code != "0":
        page.error = f"provider: headerCd={page.provider_code} {page.header.get('headerMsg') or ''}".strip()
        return page
    page.items = [{_local(child.tag): (child.text or "").strip() for child in element}
                  for element in root.iter() if _local(element.tag) == "itemList" and len(element) > 0]
    current, total_pages = page.header.get("currentPage"), page.header.get("itemPageCnt")
    if current and total_pages and current.isdigit() and total_pages.isdigit() and int(total_pages) > int(current):
        page.status = "partial"
        page.error = f"partial: itemPageCnt={total_pages}, currentPage={current}"
        return page
    page.status = "success" if page.items else "empty"
    return page


def _decode(body: bytes, content_type: str | None) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    try:
        return body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
