"""ODsay 대중교통 API(api.odsay.com) 클라이언트 — 버스 노선 지도 형상 수집용.

쓰는 기능은 두 가지이고, 아래 내용은 2026-11-12 실응답으로 확인했다.
- searchBusLane(busNo, CID): 노선번호가 busNo로 시작하는 노선 목록(예: busNo=60 → 60, 601~608). lane[].busID는 ODsay 노선 ID,
  lane[].localBusID는 지역 노선 ID로 대전은 TAGO 노선 ID(DJB…)와 같다. startNO는 건너뛸 결과 수여서 1을 보내면 첫 결과가
  빠지고(실측), totalCount는 전체 수가 아니라 받은 수와 같게 왔다. 그래서 startNO를 보내지 않고 displayCnt를 넉넉히 준다.
- loadLane(mapObject): 노선 그래픽 데이터. mapObject=0:0@{busID}:1:-1:-1@{busID}:1:-1:-1… 로 한 요청에 여러 노선을 넣을 수 있고
  result.lane[]이 요청 순서대로 온다. lane에는 노선 ID가 없으므로 개수가 요청과 다르면 순서만으로는 어느 노선의 선인지
  알 수 없다(partial, 받은 선은 그대로 돌려주고 호출자가 정류소 대응으로 노선을 가린다).
  lane[].section[].graphPos[]의 x·y는 경위도(WGS84)이며 전체 노선(-1:-1)은 기점→회차→종점이 선 하나다.

오류는 HTTP 200 본문의 error(객체 또는 배열, code·message/msg)로도 온다. 요청 1회에 HTTP 1회이고 재시도하지 않는다
(일일 호출 한도가 작아 다음 수집 때 다시 시도한다). 인증키는 요청 직전에만 쓰고 본문·오류 메시지에 되돌아오면 가린다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

BASE_URL = "https://api.odsay.com/v1/api"
SEARCH_BUS_LANE = "searchBusLane"
LOAD_LANE = "loadLane"
BUS_CLASS = "1"

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_API_KEY_PARAM = re.compile(r"(apiKey=)[^&\s\"'<>]*", re.IGNORECASE)


class OdsayConfigError(RuntimeError):
    """인증키가 없다. 메시지에 키 값을 넣지 않는다."""


@dataclass
class OdsayCall:
    """HTTP 요청 1회의 기록. status: success / empty / partial / error / timeout."""

    operation: str
    params: dict[str, str]
    requested_at: datetime
    received_at: datetime | None = None
    http_status: int | None = None
    status: str = "error"
    provider_code: str | None = None
    error: str | None = None
    body: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "empty")


@dataclass(frozen=True)
class BusLane:
    """노선 검색 결과 1건. local_bus_id가 지역(TAGO) 노선 ID다."""

    bus_id: str
    bus_no: str | None
    local_bus_id: str | None
    city_code: str | None


@dataclass
class LaneSearch:
    call: OdsayCall
    total_count: int | None = None
    lanes: list[BusLane] = field(default_factory=list)


@dataclass
class LaneShapes:
    """노선 그래픽 조회 결과. 선 하나는 [경도, 위도] 목록이다.

    status가 success면 lines는 요청한 bus_ids와 같은 순서·개수다. partial이면 개수가 달라 순서로 짝지을 수 없다.
    """

    call: OdsayCall
    bus_ids: tuple[str, ...] = ()
    lines: list[list[list[float]]] = field(default_factory=list)


class OdsayClient:
    def __init__(self, api_key: str, *, timeout: float = 20.0, transport: httpx.BaseTransport | None = None) -> None:
        key = (api_key or "").strip()
        if not key:
            raise OdsayConfigError("ODsay API 키가 비어 있습니다.")
        self._api_key = key
        variants = {key, quote(key, safe=""), quote_plus(key), str(httpx.QueryParams({"k": key}))[2:]}
        self._key_pattern = re.compile("|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)))
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> OdsayClient:
        if settings.odsay_api_key is None:
            raise OdsayConfigError("ODSAY_API_KEY가 설정되지 않았습니다. .env.api에 ODsay 서버 키를 입력하세요.")
        return cls(settings.odsay_api_key.get_secret_value(),
                   **{"timeout": settings.odsay_timeout_seconds, **options})

    def __enter__(self) -> OdsayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        return self._key_pattern.sub("***", _API_KEY_PARAM.sub(r"\1***", text))

    def search_bus_lanes(self, bus_no: str, *, city_code: str, display_count: int = 200) -> LaneSearch:
        """노선번호가 bus_no로 시작하는 노선 목록(최대 display_count개). 받은 수가 display_count면 잘렸을 수 있다."""
        params = {"busNo": bus_no, "CID": city_code, "displayCnt": str(display_count)}
        call, result = self._get(SEARCH_BUS_LANE, params)
        search = LaneSearch(call=call)
        if result is None:
            return search
        raw_lanes = result.get("lane")
        if raw_lanes is None:
            raw_lanes = []
        if not isinstance(raw_lanes, list):
            return _fail(search, "parse: result.lane이 목록이 아님")
        for item in raw_lanes:
            if not isinstance(item, dict) or item.get("busID") in (None, ""):
                return _fail(search, "parse: lane 항목에 busID가 없음")
            search.lanes.append(BusLane(bus_id=str(item["busID"]), bus_no=_text(item.get("busNo")),
                                        local_bus_id=_text(item.get("localBusID")),
                                        city_code=_text(item.get("busCityCode"))))
        total = result.get("totalCount")
        search.total_count = total if isinstance(total, int) and not isinstance(total, bool) else None
        call.status = "success" if search.lanes else "empty"
        return search

    def load_lanes(self, bus_ids: Sequence[str]) -> LaneShapes:
        """노선 전체 형상을 한 요청으로 받는다. 받은 선의 수가 요청과 다르면 partial(선이 없으면 empty)이다."""
        ids = tuple(str(i) for i in bus_ids)
        if not ids:
            raise ValueError("bus_ids가 비어 있습니다.")
        map_object = "0:0" + "".join(f"@{bus_id}:{BUS_CLASS}:-1:-1" for bus_id in ids)
        call, result = self._get(LOAD_LANE, {"mapObject": map_object})
        shapes = LaneShapes(call=call, bus_ids=ids)
        if result is None:
            return shapes
        raw_lanes = result.get("lane")
        if raw_lanes is None:
            raw_lanes = []
        if not isinstance(raw_lanes, list):
            return _fail(shapes, "parse: result.lane이 목록이 아님")
        lines: list[list[list[float]]] = []
        for lane in raw_lanes:
            line = _lane_line(lane)
            if line is None:
                return _fail(shapes, "parse: lane 좌표를 해석할 수 없음")
            lines.append(line)
        shapes.lines = lines
        if len(lines) != len(ids):
            call.status = "partial" if lines else "empty"
            call.error = f"partial: 요청 노선 {len(ids)}개, 응답 lane {len(lines)}개"
            return shapes
        call.status = "success"
        return shapes

    def _get(self, operation: str, params: dict[str, str]) -> tuple[OdsayCall, dict[str, Any] | None]:
        call = OdsayCall(operation=operation, params=dict(params), requested_at=_utc_now())
        try:
            response = self._http.get(f"/{operation}", params={"apiKey": self._api_key, **params})
        except httpx.TimeoutException as exc:
            call.status, call.error = "timeout", f"timeout: 응답 제한시간 초과 ({type(exc).__name__})"
            return call, None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            call.error = f"network: {type(exc).__name__}: {self.redact(str(exc))}"
            return call, None
        call.received_at = _utc_now()
        call.http_status = response.status_code
        call.body = self.redact(response.text)
        try:
            data = json.loads(call.body)
        except ValueError:
            data = None
        error = _error_of(data)
        if error is not None:
            call.provider_code, message = error
            call.error = f"provider: {call.provider_code or ''} {message}".strip()
            return call, None
        if not 200 <= response.status_code < 300:
            call.error = f"http: HTTP {response.status_code}"
            return call, None
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            call.error = "parse: result가 없는 응답"
            return call, None
        return call, result


def _fail(result: Any, message: str) -> Any:
    result.call.status, result.call.error = "error", message
    return result


def _error_of(data: Any) -> tuple[str | None, str] | None:
    """본문의 error(객체 또는 배열)를 (코드, 메시지)로. 없으면 None."""
    if not isinstance(data, dict) or "error" not in data:
        return None
    error = data["error"]
    first = error[0] if isinstance(error, list) and error else error
    if isinstance(first, dict):
        return _text(first.get("code")), str(first.get("message") or first.get("msg") or "")
    return None, str(first)


def _lane_line(lane: Any) -> list[list[float]] | None:
    if not isinstance(lane, dict) or not isinstance(lane.get("section"), list):
        return None
    line: list[list[float]] = []
    for section in lane["section"]:
        points = section.get("graphPos") if isinstance(section, dict) else None
        if not isinstance(points, list):
            return None
        for point in points:
            if not isinstance(point, dict):
                return None
            x, y = point.get("x"), point.get("y")
            if not _number(x) or not _number(y) or not (-180 <= x <= 180 and -90 <= y <= 90):
                return None
            if not line or line[-1] != [float(x), float(y)]:
                line.append([float(x), float(y)])
    return line


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
