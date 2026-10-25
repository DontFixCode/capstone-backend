"""TAGO(국토교통부) 버스정보 API 호출과 응답 해석을 담당한다.

정류소·노선·위치·도착 서비스의 7개 상세기능만 다룬다. 인증키는 공공데이터포털 일반 인증키(Decoding)를
받아 httpx 쿼리 인코딩으로 한 번만 인코딩한다. 인증키가 담긴 URL·예외 문자열은 로그·오류·반환값에
남기지 않는다. 오류는 빈 목록이나 성공으로 바꾸지 않고 상태·분류와 함께 반환한다.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

BASE_URL = "https://apis.data.go.kr/1613000"
DAEJEON_CITY_CODE = "25"

# httpx·httpcore는 INFO·DEBUG 로그에 serviceKey가 포함된 요청 URL을 남기므로 경고 이상만 출력한다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Endpoint:
    """상세기능 하나. documented_fields는 공식 명세의 응답 item 항목이다."""

    service: str
    operation: str
    paged: bool
    documented_fields: tuple[str, ...]

    @property
    def path(self) -> str:
        return f"{self.service}/{self.operation}"


STATION_LIST = Endpoint(
    "BusSttnInfoInqireService", "getSttnNoList", True,
    ("gpslati", "gpslong", "nodeid", "nodenm", "nodeno"),
)
NEARBY_STATIONS = Endpoint(
    "BusSttnInfoInqireService", "getCrdntPrxmtSttnList", True,
    ("gpslati", "gpslong", "nodeid", "nodenm", "citycode"),
)
ROUTE_LIST = Endpoint(
    "BusRouteInfoInqireService", "getRouteNoList", True,
    ("routeid", "routeno", "routetp", "endnodenm", "startnodenm", "endvehicletime", "startvehicletime"),
)
ROUTE_INFO = Endpoint(
    "BusRouteInfoInqireService", "getRouteInfoIem", False,
    (
        "routeid", "routeno", "routetp", "endnodenm", "startnodenm", "endvehicletime", "startvehicletime",
        "intervaltime", "intervalsattime", "intervalsuntime",
    ),
)
ROUTE_STOPS = Endpoint(
    "BusRouteInfoInqireService", "getRouteAcctoThrghSttnList", True,
    ("routeid", "nodeid", "nodenm", "nodeno", "nodeord", "gpslati", "gpslong", "updowncd"),
)
BUS_LOCATIONS = Endpoint(
    "BusLcInfoInqireService", "getRouteAcctoBusLcList", True,
    ("routenm", "gpslati", "gpslong", "nodeord", "nodenm", "nodeid", "routetp", "vehicleno"),
)
ARRIVALS = Endpoint(
    "ArvlInfoInqireService", "getSttnAcctoArvlPrearngeInfoList", True,
    ("nodeid", "nodenm", "routeid", "routeno", "routetp", "arrprevstationcnt", "vehicletp", "arrtime"),
)
ENDPOINTS = {
    endpoint.operation: endpoint
    for endpoint in (STATION_LIST, NEARBY_STATIONS, ROUTE_LIST, ROUTE_INFO, ROUTE_STOPS, BUS_LOCATIONS, ARRIVALS)
}

# 공식 오류코드 표 기준. 게이트웨이 오류는 XML(returnReasonCode), 제공기관 오류는 header.resultCode로 온다.
# 일시 오류로 안내된 01(GW 내부 오류)·05(연계 시간초과)만 재시도한다.
_RETRYABLE_CODES = frozenset({"01", "05"})
_AUTH_CODES = frozenset({"20", "29", "30", "31", "32"})
_RATE_LIMIT_CODES = frozenset({"22", "23"})
_BAD_REQUEST_CODES = frozenset({"10", "12"})

_RAW_TEXT_LIMIT = 4000
_SERVICE_KEY_PARAM = re.compile(r"(serviceKey=)[^&\s\"'<>]*", re.IGNORECASE)
_URL_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}")
_MISSING = object()


class TagoConfigError(RuntimeError):
    """실제 호출에 필요한 인증키가 없거나 형식이 맞지 않다."""


@dataclass
class TagoError:
    """요청 하나의 실패 원인. category로 시간초과·HTTP·인증·한도·제공기관 오류 등을 구분한다."""

    category: str
    message: str
    retryable: bool
    http_status: int | None = None
    code: str | None = None
    source: str | None = None
    excerpt: str | None = None


@dataclass
class Attempt:
    """실제 HTTP 시도 하나. 응답을 받지 못했으면 received_at은 None이다."""

    started_at: str
    received_at: str | None
    outcome: str
    http_status: int | None


@dataclass
class Page:
    """요청 하나(재시도 포함)의 결과. params에는 인증키를 넣지 않는다."""

    params: dict[str, str]
    status: str
    attempts: list[Attempt]
    requested_at: str | None
    received_at: str | None
    header: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    error: TagoError | None = None
    budget_exhausted: bool = False
    # 인증키를 가린 원본 응답. JSON이면 해석한 객체, 아니면 텍스트 앞부분이며 응답을 받지 못하면 None이다.
    raw: Any = None

    @property
    def total_count(self) -> int | None:
        return to_int(self.meta.get("totalCount"))


@dataclass
class Collection:
    """한 조회 조건의 전체 결과. status는 success·empty·partial·failed 중 하나다."""

    endpoint: Endpoint
    params: dict[str, str]
    rows: int | None
    pages: list[Page]
    items: list[dict[str, Any]]
    status: str
    stop_reason: str | None
    total_count: int | None
    warnings: list[str]

    @property
    def error(self) -> TagoError | None:
        return self.pages[-1].error if self.pages else None


@dataclass
class _Parsed:
    header: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    error: TagoError | None = None
    raw: Any = None


class TagoClient:
    """TAGO 상세기능 호출 클라이언트. 요청은 순차 실행하며 모든 HTTP 시도를 예산과 통계에 반영한다."""

    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 10.0,
        request_interval: float = 0.5,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        max_requests: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = (service_key or "").strip()
        if not key:
            raise TagoConfigError("TAGO_SERVICE_KEY가 비어 있습니다.")
        if _URL_ENCODED.search(key):
            raise TagoConfigError(
                "TAGO_SERVICE_KEY에 URL 인코딩된 값(%XX)이 있습니다. 공공데이터포털의 일반 인증키(Decoding)를 입력하세요."
            )
        self._service_key = key
        variants = {key, quote(key, safe=""), quote_plus(key), str(httpx.QueryParams({"k": key}))[2:]}
        self._key_pattern = re.compile(
            "|".join(re.escape(variant) for variant in sorted(variants, key=len, reverse=True)), re.IGNORECASE
        )
        self.request_interval = request_interval
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_requests = max_requests
        self.request_count = 0
        self.stats: dict[str, dict[str, int]] = {}
        self._sleep = sleep
        self._last_request_end: float | None = None
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> TagoClient:
        if settings.tago_service_key is None:
            raise TagoConfigError(
                "TAGO_SERVICE_KEY가 설정되지 않았습니다. 저장소 루트의 .env.api 또는 프로세스 환경변수에 "
                "공공데이터포털 일반 인증키(Decoding)를 입력하세요."
            )
        configured: dict[str, Any] = {
            "timeout": settings.tago_timeout_seconds,
            "request_interval": settings.tago_request_interval_seconds,
            "max_retries": settings.tago_max_retries,
            "retry_backoff": settings.tago_retry_backoff_seconds,
        }
        return cls(settings.tago_service_key.get_secret_value(), **{**configured, **options})

    def __enter__(self) -> TagoClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def remaining_requests(self) -> int | None:
        if self.max_requests is None:
            return None
        return max(self.max_requests - self.request_count, 0)

    def redact(self, text: str) -> str:
        """인증키와 serviceKey 쿼리값을 가린다."""
        return self._key_pattern.sub("***", _SERVICE_KEY_PARAM.sub(r"\1***", text))

    def station_list(
        self, city_code: str = DAEJEON_CITY_CODE, *, node_name: str | None = None, node_no: str | None = None,
        rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        params = {"cityCode": city_code, "nodeNm": node_name, "nodeNo": node_no}
        return self._collect(STATION_LIST, params, rows, max_pages)

    def nearby_stations(
        self, latitude: float | str, longitude: float | str, *, rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        return self._collect(NEARBY_STATIONS, {"gpsLati": latitude, "gpsLong": longitude}, rows, max_pages)

    def route_list(
        self, city_code: str = DAEJEON_CITY_CODE, *, route_no: str | None = None,
        rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        return self._collect(ROUTE_LIST, {"cityCode": city_code, "routeNo": route_no}, rows, max_pages)

    def route_info(self, route_id: str, city_code: str = DAEJEON_CITY_CODE) -> Collection:
        return self._collect(ROUTE_INFO, {"cityCode": city_code, "routeId": route_id})

    def route_stops(
        self, route_id: str, city_code: str = DAEJEON_CITY_CODE, *, rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        return self._collect(ROUTE_STOPS, {"cityCode": city_code, "routeId": route_id}, rows, max_pages)

    def bus_locations(
        self, route_id: str, city_code: str = DAEJEON_CITY_CODE, *, rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        return self._collect(BUS_LOCATIONS, {"cityCode": city_code, "routeId": route_id}, rows, max_pages)

    def arrivals(
        self, node_id: str, city_code: str = DAEJEON_CITY_CODE, *, rows: int = 100, max_pages: int | None = None,
    ) -> Collection:
        return self._collect(ARRIVALS, {"cityCode": city_code, "nodeId": node_id}, rows, max_pages)

    def fetch_page(self, endpoint: Endpoint, params: dict[str, Any], *, page_no: int = 1, rows: int = 10) -> Page:
        """지정한 한 페이지만 요청하고 페이지를 순회하지 않는다. 페이지를 지원하지 않는 상세기능은 page_no·rows를 쓰지 않는다."""
        query = _clean_params(params)
        if endpoint.paged:
            if page_no < 1 or rows < 1:
                raise ValueError("pageNo와 numOfRows는 1 이상이어야 합니다.")
            query.update(pageNo=str(page_no), numOfRows=str(rows))
        return self._request(endpoint, query)

    def _collect(
        self, endpoint: Endpoint, params: dict[str, Any], rows: int | None = None, max_pages: int | None = None,
    ) -> Collection:
        """페이지를 순회한다. 빈 페이지·반복 페이지·비정상 응답·제한 도달 시 멈추고 사유를 남긴다."""
        if endpoint.paged and (rows is None or rows < 1):
            raise ValueError("numOfRows는 1 이상이어야 합니다.")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages는 1 이상이어야 합니다.")
        base = _clean_params(params)
        pages: list[Page] = []
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen: set[str] = set()
        total: int | None = None
        stop: str | None = None
        page_no = 1
        while True:
            query = dict(base)
            if endpoint.paged:
                query.update(pageNo=str(page_no), numOfRows=str(rows))
            page = self._request(endpoint, query)
            pages.append(page)
            if page.error:
                stop = "budget_exhausted" if page.budget_exhausted else "error"
                break
            if not endpoint.paged:
                items.extend(page.items)
                break
            if page_no == 1:
                total = page.total_count
            elif page.total_count != total:
                warnings.append(f"totalCount가 {total}에서 {page.total_count}(으)로 바뀜 (pageNo={page_no})")
            echoed = to_int(page.meta.get("pageNo"))
            if echoed is not None and echoed != page_no:
                stop = "page_number_mismatch"
                break
            if not page.items:
                if total and len(items) < total:
                    stop = "empty_page_before_total"
                elif total is None:
                    warnings.append("빈 응답에 totalCount가 없음")
                break
            signature = json.dumps(page.items, sort_keys=True, ensure_ascii=False)
            if signature in seen:
                stop = "repeated_page"
                break
            seen.add(signature)
            items.extend(page.items)
            if total is None:
                stop = "total_count_missing"
                break
            if len(items) >= total:
                if len(items) > total:
                    warnings.append(f"수집 항목 {len(items)}개가 totalCount {total}보다 많음")
                break
            if len(page.items) < rows:
                stop = "short_page"
                break
            if max_pages is not None and page_no >= max_pages:
                stop = "page_limit"
                break
            page_no += 1
        if stop is None:
            status = "success" if items else "empty"
        else:
            status = "partial" if items else "failed"
        return Collection(endpoint, base, rows if endpoint.paged else None, pages, items, status, stop, total, warnings)

    def _request(self, endpoint: Endpoint, params: dict[str, str]) -> Page:
        """요청 하나를 보내고 일시 오류만 제한적으로 재시도한다. 모든 시도를 예산에 포함한다."""
        query = {"serviceKey": self._service_key, "_type": "json", **params}
        stats = self.stats.setdefault(
            endpoint.path,
            {"http_attempts": 0, "retries": 0, "succeeded": 0, "empty": 0, "failed": 0, "not_sent": 0},
        )
        attempts: list[Attempt] = []
        parsed = _Parsed()
        budget_exhausted = False
        while True:
            if self.max_requests is not None and self.request_count >= self.max_requests:
                budget_exhausted = True
                if not attempts:
                    parsed = _Parsed(error=TagoError("budget", "실행당 최대 요청 수에 도달해 요청하지 않았습니다.", False))
                else:
                    parsed.error.message += " (최대 요청 수에 도달해 재시도하지 않음)"
                break
            self._pause(len(attempts))
            started_at = _utc_now()
            received_at = None
            http_status = None
            self.request_count += 1
            stats["http_attempts"] += 1
            if attempts:
                stats["retries"] += 1
            try:
                response = self._http.get(f"/{endpoint.path}", params=query)
            except httpx.TimeoutException as exc:
                parsed = _Parsed(error=TagoError("timeout", f"응답 제한시간 초과 ({type(exc).__name__})", True))
            except httpx.TransportError as exc:
                message = f"연결 오류 ({type(exc).__name__}): {self.redact(str(exc))}"
                parsed = _Parsed(error=TagoError("network", message, True))
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                parsed = _Parsed(error=TagoError("http", f"요청 처리 오류 ({type(exc).__name__})", False))
            else:
                received_at = _utc_now()
                http_status = response.status_code
                parsed = self._interpret(response)
            finally:
                self._last_request_end = time.monotonic()
            outcome = parsed.error.category if parsed.error else "ok"
            attempts.append(Attempt(started_at, received_at, outcome, http_status))
            if parsed.error is None or not parsed.error.retryable or len(attempts) > self.max_retries:
                break

        if parsed.error is None:
            stats["succeeded"] += 1
            if not parsed.items:
                stats["empty"] += 1
        elif attempts:
            stats["failed"] += 1
        else:
            stats["not_sent"] += 1
        status = "error" if parsed.error else ("ok" if parsed.items else "empty")
        return Page(
            params=dict(params),
            status=status,
            attempts=attempts,
            requested_at=attempts[0].started_at if attempts else None,
            received_at=attempts[-1].received_at if attempts else None,
            header=parsed.header,
            meta=parsed.meta,
            items=parsed.items,
            error=parsed.error,
            budget_exhausted=budget_exhausted,
            raw=parsed.raw,
        )

    def _pause(self, retry_number: int) -> None:
        wait = self.request_interval
        if retry_number:
            wait = max(wait, self.retry_backoff * 2 ** (retry_number - 1))
        if self._last_request_end is None:
            return
        remaining = wait - (time.monotonic() - self._last_request_end)
        if remaining > 0:
            self._sleep(remaining)

    def _interpret(self, response: httpx.Response) -> _Parsed:
        """HTTP 상태와 TAGO resultCode를 따로 확인하고 items를 목록으로 정리한다."""
        text = self.redact(response.text).lstrip("﻿")
        status = response.status_code
        excerpt = text.strip()[:300] or None
        raw_text = text.strip()[:_RAW_TEXT_LIMIT] or None
        xml_error = _xml_error(text) if text.lstrip().startswith("<") else None
        if not 200 <= status < 300:
            if xml_error and xml_error[1] != "00":
                return _Parsed(error=_coded_error(*xml_error, http_status=status, excerpt=excerpt), raw=raw_text)
            return _Parsed(error=_http_error(status, excerpt), raw=raw_text)
        if xml_error:
            if xml_error[1] != "00":
                return _Parsed(error=_coded_error(*xml_error, http_status=status, excerpt=excerpt), raw=raw_text)
            return _Parsed(error=_unexpected("JSON을 요청했으나 XML 정상 응답을 받음", status, excerpt), raw=raw_text)
        try:
            data = json.loads(text)
        except ValueError:
            return _Parsed(error=_unexpected("JSON으로 해석할 수 없는 응답", status, excerpt), raw=raw_text)

        response_part = data.get("response") if isinstance(data, dict) else None
        header = response_part.get("header") if isinstance(response_part, dict) else None
        if not isinstance(header, dict) or "resultCode" not in header:
            return _Parsed(error=_unexpected("response.header.resultCode가 없음", status, None), raw=data)
        code = _normalize_code(header["resultCode"])
        if code != "00":
            message = str(header.get("resultMsg") or "")
            return _Parsed(header=header, error=_coded_error("provider", code, message, status, None), raw=data)
        body = response_part.get("body", _MISSING)
        if not isinstance(body, dict):
            return _Parsed(header=header, error=_unexpected("정상 응답에 body 객체가 없음", status, None), raw=data)
        meta = {name: body[name] for name in ("numOfRows", "pageNo", "totalCount") if name in body}
        items = _extract_items(body.get("items", _MISSING))
        if items is None:
            return _Parsed(
                header=header, meta=meta, error=_unexpected("items 구조를 해석할 수 없음", status, None), raw=data
            )
        return _Parsed(header=header, meta=meta, items=items, raw=data)


def field_state(item: dict[str, Any], name: str) -> str:
    """선택 필드의 누락(missing)·null·빈값(empty)·실제 0(zero)·값 있음(present)을 구분한다."""
    if name not in item:
        return "missing"
    value = item[name]
    if value is None:
        return "null"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "empty"
        return "zero" if re.fullmatch(r"[+-]?0+(\.0+)?", text) else "present"
    if isinstance(value, (dict, list)):
        return "present" if value else "empty"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return "zero"
    return "present"


def text_value(item: dict[str, Any], name: str) -> str | None:
    """ID·정류소번호·차량번호 등을 문자열로 읽는다. 누락·null·빈값은 None이고 0은 "0"으로 보존한다."""
    value = item.get(name)
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    return str(value).strip() or None


def to_int(value: Any) -> int | None:
    """정수 또는 정수 문자열만 변환한다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _clean_params(params: dict[str, Any]) -> dict[str, str]:
    """값이 없는 선택 조건은 보내지 않는다."""
    return {name: str(value) for name, value in params.items() if value is not None and str(value) != ""}


def _extract_items(container: Any) -> list[dict[str, Any]] | None:
    """items가 비었거나 item이 단일 객체·목록인 경우를 목록으로 바꾼다. 해석할 수 없으면 None."""
    if container is _MISSING or container is None or container == {}:
        return []
    if isinstance(container, str):
        return [] if not container.strip() else None
    if not isinstance(container, dict) or "item" not in container:
        return None
    item = container["item"]
    if item is None or item == [] or (isinstance(item, str) and not item.strip()):
        return []
    if isinstance(item, dict):
        return [item]
    if isinstance(item, list) and all(isinstance(entry, dict) for entry in item):
        return item
    return None


def _xml_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", text)
    return match.group(1) if match else None


def _xml_error(text: str) -> tuple[str, str, str] | None:
    """XML 응답에서 (출처, 코드, 메시지)를 읽는다. 포털 게이트웨이 오류는 XML로만 온다."""
    code = _xml_tag(text, "returnReasonCode")
    if code is not None:
        message = " / ".join(part for part in (_xml_tag(text, "returnAuthMsg"), _xml_tag(text, "errMsg")) if part)
        return "gateway", _normalize_code(code), message
    code = _xml_tag(text, "resultCode")
    if code is not None:
        return "provider", _normalize_code(code), _xml_tag(text, "resultMsg") or ""
    return None


def _normalize_code(value: Any) -> str:
    code = str(value).strip()
    return code.zfill(2) if code.isdigit() else code


def _coded_error(source: str, code: str, message: str, http_status: int | None, excerpt: str | None) -> TagoError:
    if code in _AUTH_CODES:
        category, retryable = "auth", False
    elif code in _RATE_LIMIT_CODES:
        category, retryable = "rate_limit", False
    elif code in _BAD_REQUEST_CODES:
        category, retryable = "bad_request", False
    else:
        category, retryable = "provider", code in _RETRYABLE_CODES
    text = f"{source} 오류 {code}" + (f": {message}" if message else "")
    return TagoError(category, text, retryable, http_status, code, source, excerpt)


def _http_error(status: int, excerpt: str | None) -> TagoError:
    if status in (401, 403):
        category, retryable = "auth", False
    elif status == 429:
        category, retryable = "rate_limit", False
    elif status >= 500:
        category, retryable = "http", True
    elif status >= 400:
        category, retryable = "bad_request", False
    else:
        category, retryable = "http", False
    message = f"HTTP {status}" + (f": {excerpt[:120]}" if excerpt else "")
    return TagoError(category, message, retryable, status, excerpt=excerpt)


def _unexpected(message: str, http_status: int, excerpt: str | None) -> TagoError:
    return TagoError("unexpected_response", message, False, http_status, excerpt=excerpt)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
