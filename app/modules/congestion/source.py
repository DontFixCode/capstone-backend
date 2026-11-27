"""대전교통빅데이터플랫폼 '노선별 정류장 승하차 인원' 내려받기 파일의 해석.

2026-11-26에 실제 파일(301번, 2026-01-01~10-22, 09시)로 확인한 형식만 받는다.

- 확장자는 .xls지만 내용은 HTML 표 하나다(`<table>` 안에 머리글 1행과 본문). 수식·매크로가 없고, 이 모듈은 셀의 글자만
  읽는다(스크립트·수식을 실행하지 않는다). 실제 엑셀 형식(.xlsx, 옛 .xls)은 확인한 적이 없어 거부한다.
- 머리글: 일자, 노선, 시간, 순번, 정류장, 단순승차, 하차, 환승승차. 총승차 열은 없다.
- 본문은 일자·노선·시간 칸을 rowspan으로 묶는다. 묶음 크기 값이 실제 행 수와 맞지 않아(합계 행을 빼고 센 값)
  rowspan을 믿지 않고, 행마다 끝의 5칸(순번·정류장·세 인원)을 읽고 앞에 붙은 칸은 내용으로 구분한다
  (`YYYY년 MM월 DD일` → 일자, `HH:00` → 시간, 그 밖 → 노선).
- 정류장 칸은 `이름(정류소 번호)`이며 번호가 빈 `이름()`도 있다(기점·회차지).
- `합계` 행은 저장하지 않고 검산에만 쓴다. 바로 앞 묶음의 합과 다르면 파일을 거부한다.
- 월별로 내려받은 파일(`YYYY년 MM월`)은 일자를 알 수 없어 거부한다.

검증에 실패한 파일은 한 행도 돌려주지 않고 사유를 모아 SourceFileError로 알린다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser

HEADERS = ("일자", "노선", "시간", "순번", "정류장", "단순승차", "하차", "환승승차")
TOTAL_LABEL = "합계"
# 실제 파일은 295일 × 1시간대가 2.6 MB였다. 한 달 전체 시간대(약 5 MB)의 열 배를 한도로 둔다.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_REPORTED_PROBLEMS = 20

_DATE = re.compile(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일$")
_MONTH = re.compile(r"^\d{4}년\s*\d{1,2}월$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_STOP = re.compile(r"^(.*)\((\d*)\)$", re.S)
_ROUTE = re.compile(r"^(.+?)번\s*(?:\((.*)\))?$", re.S)
_NUMBER = re.compile(r"^\d{1,3}(,\d{3})*$|^\d+$")


class SourceFileError(ValueError):
    """파일을 받을 수 없다. reasons에 사람이 읽을 사유를 담는다."""

    def __init__(self, file_name: str, reasons: list[str]) -> None:
        super().__init__(f"{file_name}: " + "; ".join(reasons))
        self.file_name = file_name
        self.reasons = reasons


@dataclass(frozen=True)
class SourceRow:
    """원본 한 행: 노선·일자·시간대·순번의 승하차 인원. 값은 파일 그대로다."""

    route_label: str
    service_date: date
    hour: int
    stop_order: int
    stop_name: str
    stop_no: str | None
    board: int
    alight: int
    transfer: int


@dataclass
class ParsedFile:
    file_name: str
    rows: list[SourceRow]
    # 검산한 합계 행 수와, 묶음을 특정하지 못해 검산하지 않은 합계 행 수.
    checked_totals: int = 0
    unchecked_totals: int = 0
    warnings: list[str] = field(default_factory=list)


def route_parts(label: str) -> tuple[str, str | None, str | None]:
    """노선 칸 `301번(봉산동기점~오월드기점지)` → (노선번호, 기점 이름, 종점 이름). 괄호가 없으면 이름은 None."""
    match = _ROUTE.match(label.strip())
    if match is None:
        return label.strip(), None, None
    ends = (match.group(2) or "").split("~", 1)
    if len(ends) != 2:
        return match.group(1).strip(), None, None
    return match.group(1).strip(), ends[0].strip() or None, ends[1].strip() or None


class _TableReader(HTMLParser):
    """표의 행과 칸의 글자만 모은다. (칸 종류, 글자) 목록의 목록."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables = 0
        self.rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._kind = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell, self._kind = [], tag

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((self._kind, "".join(self._cell).strip()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _number(text: str) -> int | None:
    value = text.strip()
    return int(value.replace(",", "")) if _NUMBER.match(value) else None


def parse_export(data: bytes, *, file_name: str) -> ParsedFile:
    """내려받은 파일 하나를 해석한다. 형식·값·검산에 문제가 있으면 SourceFileError."""
    if len(data) > MAX_FILE_BYTES:
        raise SourceFileError(file_name, [f"파일이 너무 큽니다({len(data)} bytes, 한도 {MAX_FILE_BYTES})."])
    if data[:2] == b"PK" or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise SourceFileError(file_name, ["실제 엑셀 형식(.xlsx·옛 .xls)은 받지 않습니다. 플랫폼의 '엑셀저장'으로 받은 "
                                          "원본 파일(HTML 표 형식 .xls)을 그대로 올리세요."])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceFileError(file_name, ["UTF-8로 읽을 수 없습니다."]) from None
    reader = _TableReader()
    reader.feed(text)
    reader.close()
    if reader.tables != 1 or not reader.rows:
        raise SourceFileError(file_name, [f"표가 1개여야 합니다(찾은 표 {reader.tables}개)."])
    header = tuple(value for _, value in reader.rows[0])
    if header != HEADERS:
        raise SourceFileError(file_name, [f"머리글이 다릅니다: {list(header)} (기대: {list(HEADERS)})."])

    problems: list[str] = []

    def problem(message: str) -> None:
        if len(problems) < MAX_REPORTED_PROBLEMS:
            problems.append(message)

    parsed = ParsedFile(file_name, [])
    seen: set[tuple[str, date, int, int]] = set()
    route: str | None = None
    day: date | None = None
    hour: int | None = None
    group = [0, 0, 0]          # 마지막 합계 행 뒤로 읽은 행의 (단순승차, 하차, 환승승차) 합
    group_rows = 0
    for number, cells in enumerate(reader.rows[1:], start=2):
        values = [value for _, value in cells]
        if not values or not any(values):
            continue
        if values[0] == TOTAL_LABEL:
            totals = [_number(value) for value in values[1:]]
            if len(totals) != 3 or any(value is None for value in totals):
                problem(f"{number}행: 합계 행의 값을 읽을 수 없습니다.")
            elif group_rows == 0:
                parsed.unchecked_totals += 1     # 합계의 합계(전체 합 등). 어느 묶음인지 형식만으로는 알 수 없다.
            elif totals != group:
                problem(f"{number}행: 합계 {totals}가 앞 {group_rows}행의 합 {group}와 다릅니다.")
            else:
                parsed.checked_totals += 1
            group, group_rows = [0, 0, 0], 0
            continue
        if len(values) < 5 or len(values) > 8:
            problem(f"{number}행: 칸 수가 맞지 않습니다({len(values)}칸).")
            continue
        for lead in values[:-5]:
            matched_date, matched_time = _DATE.match(lead), _TIME.match(lead)
            if matched_date:
                try:
                    day = date(int(matched_date.group(1)), int(matched_date.group(2)), int(matched_date.group(3)))
                except ValueError:
                    day = None
                    problem(f"{number}행: 없는 날짜입니다({lead}).")
            elif _MONTH.match(lead):
                raise SourceFileError(file_name, ["월별로 내려받은 파일입니다. 일자선택을 '일별'로 두고 다시 받으세요."])
            elif matched_time:
                if matched_time.group(2) != "00" or int(matched_time.group(1)) > 23:
                    hour = None
                    problem(f"{number}행: 1시간 단위 시간대가 아닙니다({lead}).")
                else:
                    hour = int(matched_time.group(1))
            elif lead:
                route = lead
        order, stop, board, alight, transfer = values[-5:]
        counts = [_number(board), _number(alight), _number(transfer)]
        if not order.isdigit() or int(order) < 1:
            problem(f"{number}행: 순번을 읽을 수 없습니다({order!r}).")
            continue
        if any(value is None for value in counts):
            problem(f"{number}행: 인원이 0 이상의 정수가 아닙니다({board!r}, {alight!r}, {transfer!r}).")
            continue
        if route is None or day is None or hour is None:
            problem(f"{number}행: 일자·노선·시간을 알 수 없는 행입니다.")
            continue
        key = (route, day, hour, int(order))
        if key in seen:
            problem(f"{number}행: 같은 노선·일자·시간·순번이 파일 안에 두 번 있습니다({day} {hour}시 {order}번).")
            continue
        seen.add(key)
        stop_match = _STOP.match(stop)
        name, stop_no = (stop_match.group(1).strip(), stop_match.group(2) or None) if stop_match else (stop, None)
        parsed.rows.append(SourceRow(route, day, hour, int(order), name, stop_no, counts[0], counts[1], counts[2]))
        group = [group[0] + counts[0], group[1] + counts[1], group[2] + counts[2]]
        group_rows += 1
    if not parsed.rows and not problems:
        problems.append("자료 행이 없습니다.")
    if problems:
        raise SourceFileError(file_name, problems)
    if group_rows:
        parsed.warnings.append(f"마지막 {group_rows}행 뒤에 합계 행이 없어 검산하지 못했습니다.")
    return parsed
