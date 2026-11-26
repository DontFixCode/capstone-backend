"""승하차 원본에서 구간별 승객 흐름과 시간당 통과 횟수를 계산한다(DB·HTTP 없음).

재차 인원을 직접 관측한 자료가 없으므로 정류장별 승하차를 경유 순서대로 누적해 구간별 흐름을 추정한다.

    L[k] = max(0, L[k-1] + 총승차[k] - α × 하차[k])     (L[0] = 0, k는 원본 순번)
    α    = Σ총승차 ÷ Σ하차                              (같은 노선·일자·시간대의 합계)

- L[k]는 그 시간대에 k번째 정류소를 출발한 직후 구간을 지난 모든 차량의 재차 합이다. 한 운행이 같은 시간대 안에서
  끝난다고 근사한다(시간 밀림 보정은 후속 범위).
- α는 하차 태그 누락과, 그 시간대에 탄 승객이 다음 시간대에 내리는 차이를 함께 보정해 시간대 안에서 탄 만큼 내리게
  맞춘다. 태그 누락이 정류장마다 고르다고 가정한다. 설계 초안은 하루 합계로 α를 구했지만, 301번 전체 시간대 자료
  (2026-09-17~11-15)에서는 그 방식이 닫히지 않았다: 평일 7시에 종점까지 시간당 약 407명이 남고(시간대별 α는 약 22명)
  9시에는 반대로 지나치게 깎였다. 하차 합이 0인데 승차가 있는 일자·시간대는 보정할 수 없어 표본에서 뺀다.
- 원본 순번을 현재 패턴의 경유 순번에 순서를 지켜 대응시킨다(정류소 내부 ID가 같은 위치끼리). 패턴에 없는 원본 정류장의
  승하차도 누적에는 그대로 들어가고, 원본에 없는 패턴 정류소는 바로 앞 구간의 값을 잇는다. 대응 비율이 낮으면
  (MIN_STOP_MATCH_SHARE 미만) 다른 경로로 바뀐 것으로 보고 집계를 보류한다.
- 요일유형은 weekday / saturday / holiday(일요일·공휴일)이며 공휴일 목록은 배포 설정이다.
- 통과 횟수는 노선 위치 관측에서 실제로 지나간 차량을 구간별·시간대별로 센 값을 쓴다(observed, observed_trips).
  배차간격은 요일별 값 하나뿐이라 시간대에 따른 증편·감편과 정체로 느려지는 것을 담지 못한다. 그 요일유형·시간대의
  관측 일수가 모자라면 노선 정보의 배차간격으로 대신한다(timetable).
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

KST = timezone(timedelta(hours=9))
DAY_TYPES = ("weekday", "saturday", "holiday")
TRIPS_TIMETABLE = "timetable"
# 원본 정류장 가운데 현재 패턴에 대응된 비율이 이 값보다 낮으면 집계를 보류한다(형상 대응 기준과 같은 개발 설정값).
MIN_STOP_MATCH_SHARE = 0.9
# 배차간격(분)으로 받아들이는 범위. 벗어나면 단위·값이 다른 것으로 보고 쓰지 않는다(검색의 배차간격 예상과 같은 범위).
HEADWAY_MINUTES = (1.0, 180.0)
_HEADWAY_FIELD = {"weekday": "intervaltime", "saturday": "intervalsattime", "holiday": "intervalsuntime"}


@dataclass(frozen=True)
class CountRow:
    """원본 한 행에서 집계에 쓰는 값."""

    service_date: date
    hour: int
    stop_order: int
    stop_id: int | None
    total_board: int
    alight: int


@dataclass(frozen=True)
class ProfileRow:
    """congestion_profiles 한 행."""

    pattern_key: str
    stop_seq: int
    day_type: str
    hour: int
    flow_mean: float
    flow_p80: float | None
    trips_per_hour: float | None
    trips_source: str | None
    sample_days: int
    source_period: str


@dataclass
class AggregateResult:
    status: str                      # computed / no_data / held
    reason: str | None
    profiles: list[ProfileRow]
    days_used: int = 0
    hours_skipped: int = 0           # 하차 합이 0인데 승차가 있어 보정할 수 없는 일자·시간대 수
    matched_stops: int = 0
    source_stops: int = 0
    pattern_stops: int = 0
    source_period: str | None = None
    alpha_median: float | None = None


def day_type(day: date, holidays: Iterable[date] = ()) -> str:
    """요일유형. 일요일과 공휴일(평일에 낀 날 포함)은 holiday다."""
    if day.weekday() == 6 or day in set(holidays):
        return "holiday"
    return "saturday" if day.weekday() == 5 else "weekday"


def window_start(now: datetime, months: int) -> date:
    """집계 시점(한국 날짜) 기준 직전 n개월의 첫날. 같은 날짜가 없는 달은 그 달의 마지막 날로 맞춘다."""
    today = now.astimezone(KST).date()
    index = today.year * 12 + (today.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    for day in range(today.day, 0, -1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError("집계 창의 시작일을 계산할 수 없습니다.")


def align_stops(source: Sequence[tuple[int, Hashable]], pattern: Sequence[tuple[int, Hashable]]) -> dict[int, int]:
    """원본 순번 → 패턴 경유 순번. 두 목록의 키(정류소)가 같은 위치를 순서를 지켜 짝짓는다.

    같은 정류소를 두 번 지나는 노선도 순서로 구분된다. 키가 None인 항목은 짝짓지 않는다.
    """
    left = [key for _, key in source]
    right = [key for _, key in pattern]
    matcher = SequenceMatcher(a=left, b=right, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            if left[block.a + offset] is not None:
                mapping[source[block.a + offset][0]] = pattern[block.b + offset][0]
    return mapping


def link_flows(rows: Sequence[tuple[int, int, int]], alpha: float) -> dict[int, float]:
    """(순번, 총승차, 하차)를 순번 순서로 누적한 구간 흐름. 반환: 순번 → 그 정류소를 출발한 직후의 값."""
    flows: dict[int, float] = {}
    load = 0.0
    for order, board, alight in sorted(rows):
        load = max(0.0, load + board - alpha * alight)
        flows[order] = load
    return flows


def percentile(values: Sequence[float], share: float) -> float:
    """선형 보간 백분위(0~1). 값이 하나면 그 값이다."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("값이 없습니다.")
    position = (len(ordered) - 1) * share
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def timetable_trips_per_hour(service_info: Mapping[str, Any] | None, kind: str) -> float | None:
    """노선 정보의 배차간격(분, 평일·토·일)으로 구한 시간당 통과 횟수. 값이 없거나 범위를 벗어나면 None.

    패턴은 기점에서 종점까지의 한 방향 경유 순서이므로, 한 구간을 지나는 횟수는 그 방향의 배차와 같다.
    """
    fields = (service_info or {}).get("fields") or {}
    try:
        minutes = float((fields.get(_HEADWAY_FIELD[kind]) or {}).get("value"))
    except (TypeError, ValueError):
        return None
    if not HEADWAY_MINUTES[0] <= minutes <= HEADWAY_MINUTES[1]:
        return None
    return 60.0 / minutes


def build_profiles(
    rows: Iterable[CountRow],
    pattern_key: str,
    pattern_stops: Sequence[tuple[int, int]],
    *,
    service_info: Mapping[str, Any] | None,
    holidays: Iterable[date] = (),
    min_sample_days: int | None = None,
    observed: ObservedTrips | None = None,
) -> AggregateResult:
    """집계 창 안의 원본 행으로 한 패턴의 프로필을 만든다. pattern_stops는 (경유 순번, 정류소 내부 ID) 목록이다.

    min_sample_days가 있으면 표본 일수가 그보다 적은 칸의 flow_p80은 NULL로 둔다(없으면 2일 이상이면 계산).
    observed에 그 요일유형·시간대의 관측이 있으면 구간별 관측 통과 횟수를 쓰고(시간당 MIN_TRIPS_PER_HOUR 미만이면 비움),
    없으면 배차간격으로 구한 값을 쓴다.
    """
    holiday_set = frozenset(holidays)
    by_day: dict[date, dict[int, list[CountRow]]] = {}
    orders: dict[int, int | None] = {}
    for row in rows:
        by_day.setdefault(row.service_date, {}).setdefault(row.hour, []).append(row)
        orders.setdefault(row.stop_order, row.stop_id)
    if not by_day:
        return AggregateResult("no_data", "no_rows_in_window", [], pattern_stops=len(pattern_stops))
    source = sorted(orders.items())
    mapping = align_stops(source, sorted(pattern_stops))
    result = AggregateResult("computed", None, [], matched_stops=len(mapping), source_stops=len(source),
                             pattern_stops=len(pattern_stops))
    if len(mapping) < MIN_STOP_MATCH_SHARE * len(source):
        result.status, result.reason = "held", "stop_alignment_below_threshold"
        return result
    seq_to_order = {seq: order for order, seq in mapping.items()}
    seqs = sorted(seq for seq, _ in pattern_stops)

    samples: dict[tuple[str, int, int], list[float]] = {}
    alphas: list[float] = []
    used: list[date] = []
    for day in sorted(by_day):
        kind = day_type(day, holiday_set)
        for hour, group in sorted(by_day[day].items()):
            boarded = sum(row.total_board for row in group)
            alighted = sum(row.alight for row in group)
            if alighted == 0 and boarded > 0:
                result.hours_skipped += 1
                continue
            alpha = boarded / alighted if alighted else 1.0
            if alighted:
                alphas.append(alpha)
            if not used or used[-1] != day:
                used.append(day)
            flows = link_flows([(row.stop_order, row.total_board, row.alight) for row in group], alpha)
            passed = sorted(flows)
            carried = 0.0
            position = 0
            for seq in seqs:
                order = seq_to_order.get(seq)
                if order is not None:
                    # 이 경유 위치에 대응한 원본 순번까지 누적한 값. 그 앞의 대응하지 못한 원본 정류장도 포함된다.
                    while position < len(passed) and passed[position] <= order:
                        carried = flows[passed[position]]
                        position += 1
                samples.setdefault((kind, hour, seq), []).append(carried)
    if not used:
        result.status, result.reason = "no_data", "no_correctable_hour"
        return result
    result.days_used = len(used)
    result.source_period = f"{used[0].isoformat()}/{used[-1].isoformat()}"
    result.alpha_median = percentile(alphas, 0.5) if alphas else None
    timetable = {kind: timetable_trips_per_hour(service_info, kind) for kind in DAY_TYPES}
    threshold = min_sample_days if min_sample_days is not None else 2
    for (kind, hour, seq), values in sorted(samples.items()):
        if observed is not None and (kind, hour) in observed.days:
            rate = observed.rates.get((kind, hour, seq), 0.0)
            trips, source = (rate, TRIPS_OBSERVED) if rate >= MIN_TRIPS_PER_HOUR else (None, None)
        else:
            trips = timetable[kind]
            source = TRIPS_TIMETABLE if trips is not None else None
        result.profiles.append(ProfileRow(
            pattern_key=pattern_key, stop_seq=seq, day_type=kind, hour=hour,
            flow_mean=sum(values) / len(values),
            flow_p80=percentile(values, 0.8) if len(values) >= threshold else None,
            trips_per_hour=trips, trips_source=source,
            sample_days=len(values), source_period=result.source_period))
    return result


# ---------------------------------------------------------------- 위치 관측 기반 통과 횟수

TRIPS_OBSERVED = "observed"
# 두 관측 사이에 차량이 지날 수 있는 정류소 수의 상한을 정하는 값(정류소 하나에 최소 이만큼 걸린다고 본다).
# 이보다 빠르게 움직인 것으로 보이는 쌍은 다른 운행이나 위치 오류로 보고 세지 않는다.
MIN_SECONDS_PER_STOP = 20.0
# 한 시간대에서 관측이 이 시간(초) 이상을 덮어야 그날 그 시간대의 값을 쓴다.
MIN_HOUR_COVERAGE_SECONDS = 2700.0
# 관측한 통과 횟수가 시간당 이 값보다 적은 구간·시간대는 1대당 재차를 만들지 않는다(차량이 거의 지나지 않는 시간대에
# 적은 승객을 아주 작은 수로 나눠 값이 부풀지 않게 한다).
MIN_TRIPS_PER_HOUR = 1.0


@dataclass(frozen=True)
class VehiclePoint:
    """노선 위치 관측 한 건: 수집 시각에 차량이 있던 경유 순번."""

    at: datetime
    vehicle_id: str
    stop_seq: int


@dataclass(frozen=True)
class ObservedTrips:
    """위치 관측으로 센 통과 횟수. days: (요일유형, 시간대) → 관측 일수, rates: (요일유형, 시간대, 경유 순번) → 시간당 횟수."""

    days: Mapping[tuple[str, int], int]
    rates: Mapping[tuple[str, int, int], float]


def observed_link_rates(snapshots: Sequence[datetime], points: Iterable[VehiclePoint], last_seq: int, *,
                        max_gap_seconds: float) -> dict[tuple[date, int], list[float]]:
    """위치 관측에서 센 (한국 날짜, 시간대)별·구간별 시간당 통과 횟수. 값 목록의 s번째(1부터)가 s번째 정류소를 출발한 구간이다.

    같은 차량의 연속 관측 두 건 사이에 지나간 구간마다 1을 더하고, 그 시간대에 관측이 덮은 시간으로 나눈다. 실제로 지나간
    차량을 세므로 배차가 촘촘한 시간대는 크게, 첫차·막차 무렵이나 차량이 아직 닿지 않은 구간은 작게(또는 0으로) 나온다.
    기점에서 대기 중인 차량은 움직이지 않아 세지 않는다.

    - snapshots는 수집 시각 목록이다(차량이 한 대도 없던 수집도 포함해야 덮은 시간을 바르게 센다).
    - 종점을 지나 다시 기점에서 나온 차량은 종점까지의 구간과 기점부터의 구간을 센다.
    - 간격이 max_gap_seconds보다 긴 쌍, MIN_SECONDS_PER_STOP보다 빠른 쌍은 세지 않는다.
    - 두 수집 사이의 값은 그 가운데 시각이 속한 시간대에 넣는다. 덮은 시간이 MIN_HOUR_COVERAGE_SECONDS보다 짧은
      날짜·시간대는 결과에 넣지 않는다.
    """
    if last_seq < 2:
        return {}

    def bucket(start: datetime, end: datetime) -> tuple[date, int]:
        middle = (start + (end - start) / 2).astimezone(KST)
        return middle.date(), middle.hour

    covered: dict[tuple[date, int], float] = {}
    ordered = sorted(set(snapshots))
    for start, end in zip(ordered, ordered[1:]):
        gap = (end - start).total_seconds()
        if 0 < gap <= max_gap_seconds:
            key = bucket(start, end)
            covered[key] = covered.get(key, 0.0) + gap

    by_vehicle: dict[str, list[VehiclePoint]] = {}
    for point in points:
        if 1 <= point.stop_seq <= last_seq:
            by_vehicle.setdefault(point.vehicle_id, []).append(point)
    crossed: dict[tuple[date, int], list[float]] = {}
    for track in by_vehicle.values():
        track.sort(key=lambda point: point.at)
        for before, after in zip(track, track[1:]):
            gap = (after.at - before.at).total_seconds()
            if not 0 < gap <= max_gap_seconds:
                continue
            if after.stop_seq >= before.stop_seq:
                links = range(before.stop_seq, after.stop_seq)
            else:
                links = (*range(before.stop_seq, last_seq), *range(1, after.stop_seq))
            if len(links) > gap / MIN_SECONDS_PER_STOP:
                continue
            counts = crossed.setdefault(bucket(before.at, after.at), [0.0] * (last_seq + 1))
            for link in links:
                counts[link] += 1.0
    rates: dict[tuple[date, int], list[float]] = {}
    for key, seconds in covered.items():
        if seconds >= MIN_HOUR_COVERAGE_SECONDS:
            counts = crossed.get(key)
            rates[key] = [0.0] * (last_seq + 1) if counts is None else [count * 3600.0 / seconds for count in counts]
    return rates


def observed_trips(snapshots: Sequence[datetime], points: Iterable[VehiclePoint], last_seq: int, *,
                   max_gap_seconds: float, holidays: Iterable[date] = (), min_days: int | None = None) -> ObservedTrips:
    """요일유형·시간대별로 관측한 날들의 구간별 통과 횟수를 평균한다. 관측 일수가 min_days보다 적은 칸은 넣지 않는다.

    마지막 경유 순번은 나가는 구간이 없으므로 바로 앞 구간의 값을 쓴다.
    """
    holiday_set = frozenset(holidays)
    grouped: dict[tuple[str, int], list[list[float]]] = {}
    for (day, hour), values in observed_link_rates(snapshots, points, last_seq,
                                                   max_gap_seconds=max_gap_seconds).items():
        grouped.setdefault((day_type(day, holiday_set), hour), []).append(values)
    threshold = min_days if min_days is not None else 1
    days: dict[tuple[str, int], int] = {}
    rates: dict[tuple[str, int, int], float] = {}
    for (kind, hour), samples in grouped.items():
        if len(samples) < threshold:
            continue
        days[(kind, hour)] = len(samples)
        for seq in range(1, last_seq + 1):
            link = min(seq, last_seq - 1)
            rates[(kind, hour, seq)] = sum(values[link] for values in samples) / len(samples)
    return ObservedTrips(days, rates)
