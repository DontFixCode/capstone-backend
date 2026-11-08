"""TMAP(SK open API) 보행자 경로 조회와 응답 해석을 담당한다.

요청: POST https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1, 헤더 appKey, JSON 본문
(startX·endX=경도, startY·endY=위도, startName·endName=UTF-8 URL 인코딩, reqCoordType·resCoordType=WGS84GEO,
searchOption, sort=index). 공식 명세(tmap-skopenapi.readme.io)는 응답 구조·오류 코드를 정의하지 않으므로 다음만
해석한다: GeoJSON FeatureCollection의 properties.totalDistance(m)·totalTime(초)과 LineString 좌표.

- 앱키는 헤더로만 보내고 로그·오류·반환값에 남기지 않는다.
- 응답을 해석하지 못하거나 HTTP·제공기관 오류면 error(조회 실패)로 반환한다. 오류 코드의 의미가 확인되지 않았으므로
  어떤 오류도 '경로 없음'으로 단정하지 않는다(경로 없음은 후보를 실행 불가로 확정하기 때문이다).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

BASE_URL = "https://apis.openapi.sk.com"
PEDESTRIAN_PATH = "/tmap/routes/pedestrian"
COORD_TYPE = "WGS84GEO"
PROVIDER = "TMAP"
_RAW_EXCERPT = 300

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TmapConfigError(RuntimeError):
    """실제 호출에 필요한 앱키가 없다."""


@dataclass(frozen=True)
class WalkQuery:
    """방향 있는 도보 조회 하나. 좌표는 WGS84 위도·경도다."""

    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    search_option: str

    def options(self) -> dict[str, str]:
        return {"searchOption": self.search_option, "reqCoordType": COORD_TYPE, "resCoordType": COORD_TYPE,
                "sort": "index"}


@dataclass
class WalkResponse:
    """조회 결과. status는 available(경로·거리·시간 확인) 또는 error(조회 실패·해석 불가)다."""

    status: str
    requested_at: datetime
    received_at: datetime | None
    http_status: int | None
    distance_m: float | None = None
    duration_seconds: float | None = None
    geometry: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    options: dict[str, str] = field(default_factory=dict)


class TmapWalkingClient:
    """TMAP 보행자 경로 클라이언트. 요청은 순차 실행하며 호출 수를 센다."""

    def __init__(
        self,
        app_key: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        key = (app_key or "").strip()
        if not key:
            raise TmapConfigError("SK_OPEN_API_KEY가 비어 있습니다.")
        self._key = key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.request_count = 0
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> TmapWalkingClient:
        if settings.tmap_app_key is None:
            raise TmapConfigError(
                "SK_OPEN_API_KEY가 설정되지 않았습니다. 저장소 루트 .env.api 또는 프로세스 환경변수에 TMAP 앱키를 입력하세요."
            )
        return cls(settings.tmap_app_key.get_secret_value(), timeout=settings.tmap_timeout_seconds, **options)

    def __enter__(self) -> TmapWalkingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return text.replace(self._key, "***")

    def route(self, query: WalkQuery) -> WalkResponse:
        body = {
            "startX": query.from_lon, "startY": query.from_lat, "endX": query.to_lon, "endY": query.to_lat,
            "startName": quote("출발"), "endName": quote("도착"), **query.options(),
        }
        requested_at = self._clock()
        self.request_count += 1
        try:
            response = self._http.post(
                PEDESTRIAN_PATH, params={"version": "1"}, json=body,
                headers={"appKey": self._key, "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            return WalkResponse("error", requested_at, None, None, error_code="timeout",
                                error_message=f"응답 제한시간 초과 ({type(exc).__name__})", options=query.options())
        except httpx.HTTPError as exc:
            return WalkResponse("error", requested_at, None, None, error_code="network",
                                error_message=f"연결 오류 ({type(exc).__name__}): {self.redact(str(exc))}"[:300],
                                options=query.options())
        received_at = self._clock()
        return parse_pedestrian_response(response.status_code, self.redact(response.text), requested_at,
                                         received_at, query.options())


def parse_pedestrian_response(
    http_status: int, text: str, requested_at: datetime, received_at: datetime, options: dict[str, str],
) -> WalkResponse:
    """응답 본문을 해석한다. 거리·시간이 유한한 0 이상 숫자이고 경로 좌표가 있을 때만 available이다."""

    def failure(code: str, message: str) -> WalkResponse:
        return WalkResponse("error", requested_at, received_at, http_status, error_code=code,
                            error_message=message[:300], options=options)

    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        data = None
    if not 200 <= http_status < 300:
        code, message = _provider_error(data)
        return failure(code or f"http_{http_status}", message or text.strip()[:_RAW_EXCERPT])
    if not isinstance(data, dict):
        return failure("unexpected_response", "JSON 객체가 아닌 응답")
    code, message = _provider_error(data)
    if code is not None:
        return failure(code, message or "")
    features = data.get("features")
    if data.get("type") != "FeatureCollection" or not isinstance(features, list) or not features:
        return failure("unexpected_response", "FeatureCollection·features가 없는 응답")
    distance = duration = None
    coordinates: list[list[float]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        if distance is None and "totalDistance" in props:
            distance, duration = _number(props.get("totalDistance")), _number(props.get("totalTime"))
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "LineString":
            for point in geometry.get("coordinates") or []:
                if (isinstance(point, list) and len(point) >= 2 and all(_number(v) is not None for v in point[:2])
                        and (not coordinates or coordinates[-1] != [float(point[0]), float(point[1])])):
                    coordinates.append([float(point[0]), float(point[1])])
    if distance is None or duration is None:
        return failure("unexpected_response", "totalDistance·totalTime이 없거나 숫자가 아님")
    if not coordinates:
        return failure("unexpected_response", "경로 좌표(LineString)가 없음")
    return WalkResponse("available", requested_at, received_at, http_status, distance_m=distance,
                        duration_seconds=duration, geometry={"type": "LineString", "coordinates": coordinates},
                        options=options)


def _provider_error(data: Any) -> tuple[str | None, str | None]:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or error.get("errorCode") or error.get("id")
        message = error.get("message") or error.get("errorMessage")
        return (str(code) if code is not None else "provider_error"), (str(message) if message is not None else None)
    return None, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None
