"""장소·주소 검색 제공처 어댑터.

자체 장소·주소 DB를 두지 않고 지도 제공처 결과를 좌표·이름·주소·제공처·외부 ID로 바꿔 전달한다(Public Data 4).
지도 제공처와 앱 SDK/백엔드 경유 방식은 프론트엔드와 확정하기 전이라(2026-11-08) 실제 제공처 어댑터는 없다.
BACKEND_PLACES_PROVIDER가 비어 있거나 구현되지 않은 이름이면 '미설정'(503)으로 처리한다.
계약 검증은 이 인터페이스를 구현한 시험용 어댑터로 한다. 정상 빈 결과·호출 제한·응답 오류·시간 초과를 구분한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


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


# 실제 지도 제공처 어댑터를 구현하면 이름 → 생성 함수를 등록한다.
PROVIDERS: dict[str, object] = {}


def provider_from_settings(settings) -> PlacesProvider:
    name = (settings.places_provider or "").strip()
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
