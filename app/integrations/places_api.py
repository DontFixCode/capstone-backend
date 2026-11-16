"""장소·주소 검색 제공처 어댑터.

자체 장소·주소 DB를 두지 않고 지도 제공처 결과를 좌표·이름·주소·제공처·외부 ID로 바꿔 전달한다(Public Data 4).
BACKEND_PLACES_PROVIDER가 비어 있거나 구현되지 않은 이름이면 '미설정'(503)으로 처리한다.
정상 빈 결과·호출 제한·응답 오류·시간 초과를 구분한다.

제공처 kakao — 카카오 로컬 API(developers.kakao.com/docs/ko/local/dev-guide, 2026-11-16 확인)
- 인증: 헤더 `Authorization: KakaoAK {REST API 키}`. 키는 헤더로만 보내고 로그·오류·응답에 남기지 않는다.
- 키워드로 장소 검색: GET https://dapi.kakao.com/v2/local/search/keyword.json (query, rect, page 1~45, size 1~15).
  documents[]: id, place_name, address_name(지번), road_address_name(도로명), x(경도)·y(위도, 문자열).
- 주소 검색: GET https://dapi.kakao.com/v2/local/search/address.json (query, page 1~45, size 1~30).
  documents[]: address_name, x·y, address{address_name}(지번), road_address{address_name}(도로명). 장소 ID는 없다.
- 검색어 하나로 두 API를 모두 호출하고 주소 결과 → 장소 결과 순으로 합친다(주소 결과는 검색어가 주소일 때만 나온다).
  둘 중 하나라도 실패하면 전체를 실패로 본다(일부만 돌려주면 빈 결과와 조회 실패를 구분할 수 없다).
- 오류 본문은 {"code", "msg"}와 {"errorType", "message"} 두 형식이 모두 온다(실응답: 검색어 길이 초과는 code -2,
  파라미터 검증 실패는 ValidationError, 키 오류는 401 AccessDeniedError). 두 형식을 모두 읽고 HTTP 상태를 먼저 본다.
  429·code -10(쿼터 초과) → 호출 제한, 401·403(앱 키·카카오맵 사용 설정·허용 IP) → 미설정, 5xx·code -1/-7/-9798 → 일시 불가,
  그 밖의 오류·해석 실패 → 응답 오류.
- 실응답으로 확인(2026-11-16): 키워드 검색은 검색어가 100자를 넘으면 400(code -2)이고 주소 검색은 길이 제한이 없었다.
  그래서 100자를 넘는 검색어는 호출하지 않고 422로 거절한다. 형식이 틀린 rect는 오류 없이 무시되므로 설정을 읽을 때 검사한다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class PlacesError(Exception):
    status_code = 502
    code = "PLACES_UPSTREAM_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PlacesNotConfigured(PlacesError):
    status_code = 503
    code = "PLACES_NOT_CONFIGURED"


class PlacesUnavailable(PlacesError):
    status_code = 503
    code = "PLACES_UNAVAILABLE"


class PlacesRateLimited(PlacesError):
    status_code = 429
    code = "PLACES_RATE_LIMITED"


class PlacesUpstreamError(PlacesError):
    status_code = 502
    code = "PLACES_UPSTREAM_ERROR"


class PlacesTimeout(PlacesError):
    status_code = 504
    code = "PLACES_TIMEOUT"


class PlacesInvalidQuery(PlacesError):
    """제공처가 받지 않는 검색어(예: 길이 초과). 요청 형식 오류(422)로 응답한다."""

    status_code = 422
    code = "VALIDATION_ERROR"


@dataclass(frozen=True)
class PlaceItem:
    name: str
    address: str | None
    latitude: float
    longitude: float
    provider: str
    external_id: str | None


class PlacesProvider(Protocol):
    name: str

    def search(self, query: str) -> list[PlaceItem]:
        """정상 빈 결과는 []. 실패는 PlacesError 하위 예외로 구분한다."""
        ...


KAKAO = "kakao"
KAKAO_BASE_URL = "https://dapi.kakao.com"
KAKAO_KEYWORD_PATH = "/v2/local/search/keyword.json"
KAKAO_ADDRESS_PATH = "/v2/local/search/address.json"
# 한 번에 받는 결과 수(1쪽만 조회). 키워드 검색은 최대값이자 기본값 15, 주소 검색은 기본값 10(최대 30)이다.
KAKAO_KEYWORD_SIZE = 15
KAKAO_ADDRESS_SIZE = 10
# 키워드 검색이 받는 검색어 최대 길이(글자 수). 넘으면 카카오가 400 {"code": -2, "msg": "Max (query) length 100"}을 준다.
KAKAO_QUERY_MAX_LENGTH = 100
_KAKAO_QUOTA_CODE = -10
_KAKAO_TEMPORARY_CODES = (-1, -7, -9798)

Rect = tuple[float, float, float, float]


def parse_rect(text: str) -> Rect:
    """'최소 경도,최소 위도,최대 경도,최대 위도'(카카오 rect 형식, WGS84)를 읽는다. 형식이 틀리면 미설정으로 본다."""
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        values = ()
    if (len(values) != 4 or not all(math.isfinite(v) for v in values)
            or not (-180 <= values[0] < values[2] <= 180 and -90 <= values[1] < values[3] <= 90)):
        raise PlacesNotConfigured("장소 검색 범위 설정(BACKEND_PLACES_SEARCH_RECT) 형식이 잘못됐습니다.")
    return values  # type: ignore[return-value]


class KakaoPlacesProvider:
    """카카오 로컬 API로 장소(키워드)와 주소를 함께 검색한다. rect를 주면 그 사각형 안의 결과만 돌려준다."""

    name = KAKAO

    def __init__(self, rest_api_key: str, *, timeout: float = 5.0, rect: Rect | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        key = (rest_api_key or "").strip()
        if not key:
            raise PlacesNotConfigured("장소 검색 제공처 키가 설정되지 않았습니다.")
        self._rect = rect
        self._http = httpx.Client(base_url=KAKAO_BASE_URL, timeout=timeout, transport=transport,
                                  headers={"Authorization": f"KakaoAK {key}"})

    def close(self) -> None:
        self._http.close()

    def search(self, query: str) -> list[PlaceItem]:
        if len(query) > KAKAO_QUERY_MAX_LENGTH:
            raise PlacesInvalidQuery(f"검색어는 {KAKAO_QUERY_MAX_LENGTH}자 이하여야 합니다.")
        keyword_params: dict[str, Any] = {"query": query, "page": 1, "size": KAKAO_KEYWORD_SIZE}
        if self._rect is not None:
            keyword_params["rect"] = ",".join(repr(v) for v in self._rect)
        places = [_kakao_place(doc) for doc in self._documents(KAKAO_KEYWORD_PATH, keyword_params)]
        addresses = validate_items([_kakao_address(doc) for doc in self._documents(
            KAKAO_ADDRESS_PATH, {"query": query, "page": 1, "size": KAKAO_ADDRESS_SIZE})])
        # 주소 검색에는 범위 파라미터가 없어 받은 결과를 같은 사각형으로 거른다.
        return [item for item in addresses if self._in_rect(item)] + places

    def _in_rect(self, item: PlaceItem) -> bool:
        if self._rect is None:
            return True
        min_lon, min_lat, max_lon, max_lat = self._rect
        return min_lon <= item.longitude <= max_lon and min_lat <= item.latitude <= max_lat

    def _documents(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = self._http.get(path, params=params)
        except httpx.TimeoutException:
            raise PlacesTimeout("장소 검색 제공처 응답 시간이 초과됐습니다.") from None
        except httpx.HTTPError as exc:
            logger.warning("카카오 로컬 API 연결 오류 %s: %s", path, type(exc).__name__)
            raise PlacesUnavailable("장소 검색 제공처에 연결할 수 없습니다.") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200:
            raise _kakao_error(path, response.status_code, data)
        documents = data.get("documents") if isinstance(data, dict) else None
        if not isinstance(documents, list) or not all(isinstance(doc, dict) for doc in documents):
            raise PlacesUpstreamError("장소 검색 제공처 응답을 해석할 수 없습니다.")
        return documents


def _kakao_error(path: str, status: int, data: Any) -> PlacesError:
    body = data if isinstance(data, dict) else {}
    code, error_type = body.get("code"), body.get("errorType")
    logger.warning("카카오 로컬 API 오류 %s: HTTP %s code=%s errorType=%s message=%s", path, status, code, error_type,
                   str(body.get("msg") or body.get("message") or "")[:200])
    if status == 429 or code == _KAKAO_QUOTA_CODE:
        return PlacesRateLimited("장소 검색 요청 한도를 초과했습니다.")
    if status in (401, 403):
        return PlacesNotConfigured("장소 검색 제공처 인증·사용 설정을 확인해야 합니다.")
    if status >= 500 or code in _KAKAO_TEMPORARY_CODES:
        return PlacesUnavailable("장소 검색 제공처를 일시적으로 사용할 수 없습니다.")
    return PlacesUpstreamError("장소 검색 제공처가 오류를 응답했습니다.")


def _kakao_place(doc: dict[str, Any]) -> PlaceItem:
    return PlaceItem(name=_text(doc.get("place_name")) or "",
                     address=_text(doc.get("road_address_name")) or _text(doc.get("address_name")),
                     latitude=_coordinate(doc.get("y")), longitude=_coordinate(doc.get("x")),
                     provider=KAKAO, external_id=_text(doc.get("id")))


def _kakao_address(doc: dict[str, Any]) -> PlaceItem:
    name = _text(doc.get("address_name")) or ""
    detail = [part.get("address_name") for part in (doc.get("road_address"), doc.get("address"))
              if isinstance(part, dict)]
    address = next((text for text in map(_text, detail) if text), None) or name or None
    return PlaceItem(name=name, address=address, latitude=_coordinate(doc.get("y")),
                     longitude=_coordinate(doc.get("x")), provider=KAKAO, external_id=None)


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _coordinate(value: Any) -> float:
    """문자열 좌표를 숫자로 바꾼다. 해석하지 못하면 NaN으로 두어 validate_items가 응답 오류로 처리하게 한다."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _kakao_from_settings(settings) -> KakaoPlacesProvider:
    if settings.kakao_rest_api_key is None:
        raise PlacesNotConfigured("장소 검색 제공처 키가 설정되지 않았습니다.")
    rect = parse_rect(settings.places_search_rect) if settings.places_search_rect else None
    return KakaoPlacesProvider(settings.kakao_rest_api_key.get_secret_value(),
                               timeout=settings.kakao_timeout_seconds, rect=rect)


# 제공처 이름 → 생성 함수.
PROVIDERS: dict[str, object] = {KAKAO: _kakao_from_settings}


def provider_from_settings(settings) -> PlacesProvider:
    name = (settings.places_provider or "").strip().lower()
    if not name:
        raise PlacesNotConfigured("장소 검색 제공처가 설정되지 않았습니다.")
    factory = PROVIDERS.get(name)
    if factory is None:
        raise PlacesNotConfigured(f"구현되지 않은 장소 검색 제공처입니다: {name}")
    return factory(settings)  # type: ignore[operator]


def validate_items(items: list[PlaceItem]) -> list[PlaceItem]:
    """제공처 결과의 좌표를 확인한다. 범위를 벗어난 좌표가 있으면 해석 실패로 본다."""
    for item in items:
        if not (isinstance(item.latitude, (int, float)) and isinstance(item.longitude, (int, float))
                and math.isfinite(item.latitude) and math.isfinite(item.longitude)
                and -90 <= item.latitude <= 90 and -180 <= item.longitude <= 180 and item.name):
            raise PlacesUpstreamError("제공처 응답의 장소 좌표·이름을 해석할 수 없습니다.")
    return items
