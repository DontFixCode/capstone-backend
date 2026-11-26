"""예상 혼잡 단계 계산(조회 시점, DB·HTTP 없음).

단계와 1대당 재차는 저장하지 않고 조회할 때 계산한다. 문턱값은 좌석 수(S)와 정원(C)이 경계다.

    1대당 재차 n = 흐름 ÷ 시간당 통과 횟수        (흐름은 flow_p80 우선, NULL이면 flow_mean)
    empty(버스 전세, 가칭)  n ≤ 0.2 × S
    relaxed(여유)           n ≤ S
    normal(보통)            n ≤ (S + C) ÷ 2
    crowded(혼잡)           n ≤ C
    very_crowded(매우 혼잡) n > C

- S·C와 최소 표본 일수는 배포 설정이다(근거 없는 기본값을 두지 않는다). 설정이 없으면 단계를 만들지 않고 사유를 준다.
- 값은 과거 승하차 집계에서 계산한 '평소 이 시간대 예상'이며 실시간 관측이 아니다.
- 해당 요일유형·시간대의 행이 없으면 ① 같은 요일유형의 앞뒤 시간대 평균 ② 평일의 같은 시간대 순으로 대신하고, 대신 쓴
  사실을 fallback에 남긴다. 그래도 없으면 미확인이다. 통과 횟수가 없는 행은 단계를 만들지 않는다.
- 버스 구간(승차~하차)은 타고 가는 구간들 가운데 1대당 재차가 가장 큰 구간의 값으로 나타낸다.
- 단계 코드(empty 등)는 API 계약이 정해지기 전의 임시 값이다.
- 혼잡 비용: 버스 구간 비용 = 탑승시간 × m(단계). 탑승시간을 넘는 부분 탑승시간 × (m − 1)을 추천 비용의 내시균형 bias로
  더한다(ride_bias_seconds). 단계별 가중치 m은 배포 설정이며 없으면 0이다. 혼잡 정보가 없는 구간은 더하지 않는다
  (붐비지 않는 것으로 본 것과 같으므로, 집계가 없는 노선이 유리해진다는 점을 추천 이유로 남긴다).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.modules.congestion.aggregation import KST, day_type

LEVELS = ("empty", "relaxed", "normal", "crowded", "very_crowded")
RULE_VERSION = "seat-capacity-5/1"
EMPTY_SEAT_SHARE = 0.2

NOT_CONFIGURED = "congestion_not_configured"
PROFILE_MISSING = "congestion_profile_missing"
TIME_MISSING = "congestion_profile_missing_for_time"
TRIPS_UNKNOWN = "congestion_trips_unknown"
TIME_UNKNOWN = "congestion_time_unknown"

# (요일유형, 시간대, 경유 순번) → 집계 행(dict: flow_mean, flow_p80, trips_per_hour, trips_source, sample_days, source_period)
Profiles = Mapping[tuple[str, int, int], Mapping[str, Any]]


@dataclass(frozen=True)
class LevelPolicy:
    seats: int
    capacity: int
    min_sample_days: int | None
    holidays: frozenset[date]
    # 단계별 탑승시간 가중치(1~5단계 순). 비어 있으면 혼잡을 추천 비용에 반영하지 않는다.
    cost_weights: tuple[float, ...] = ()

    @property
    def version(self) -> str:
        body = json.dumps([RULE_VERSION, self.seats, self.capacity, self.min_sample_days, list(self.cost_weights)],
                          separators=(",", ":"))
        return "cg-" + hashlib.sha256(body.encode()).hexdigest()[:12]


def policy_from_settings(settings: Any) -> LevelPolicy | None:
    """배포 설정의 좌석 수·정원으로 정책을 만든다. 둘 중 하나라도 없거나 정원이 좌석 수보다 작으면 None."""
    seats, capacity = settings.congestion_seats, settings.congestion_capacity
    if seats is None or capacity is None or capacity < seats:
        return None
    return LevelPolicy(seats, capacity, settings.congestion_min_sample_days, frozenset(settings.congestion_holidays),
                       tuple(settings.congestion_cost_weights))


def ride_bias_seconds(congestion: Mapping[str, Any] | None, ride_seconds: float | None,
                      policy: LevelPolicy | None) -> float | None:
    """버스 구간 하나의 혼잡 비용(초) = 탑승시간 × (단계 가중치 − 1). 반영하지 못하면 None.

    가중치 설정이 없거나, 그 구간의 혼잡 단계나 탑승시간을 모르면 None이다(0과 구분한다).
    """
    if policy is None or not policy.cost_weights or ride_seconds is None:
        return None
    index = (congestion or {}).get("level_index")
    if not index:
        return None
    return ride_seconds * (policy.cost_weights[index - 1] - 1.0)


def level_of(load: float, policy: LevelPolicy) -> int:
    """1대당 재차의 단계 번호(1~5)."""
    bounds = (EMPTY_SEAT_SHARE * policy.seats, policy.seats, (policy.seats + policy.capacity) / 2, policy.capacity)
    return 1 + sum(1 for bound in bounds if load > bound)


def unavailable(reason: str, policy: LevelPolicy | None = None) -> dict[str, Any]:
    return {"status": "unavailable", "level": None, "level_index": None, "load_per_vehicle": None, "reason": reason,
            "policy_version": policy.version if policy else None}


def _rows_for(profiles: Profiles, kind: str, hour: int, seqs: Iterable[int]) -> list[tuple[int, Mapping[str, Any]]]:
    return [(seq, profiles[(kind, hour, seq)]) for seq in seqs if (kind, hour, seq) in profiles]


def _load(row: Mapping[str, Any]) -> tuple[float, str] | None:
    """(1대당 재차, 사용한 값). 통과 횟수가 없으면 None."""
    trips = row.get("trips_per_hour")
    if not trips:
        return None
    basis = "p80" if row.get("flow_p80") is not None else "mean"
    return (row["flow_p80"] if basis == "p80" else row["flow_mean"]) / trips, basis


def describe(profiles: Profiles | None, policy: LevelPolicy | None, *, at: datetime | None, from_seq: int,
             to_seq: int | None = None) -> dict[str, Any]:
    """경유 순번 from_seq(포함)부터 to_seq(제외)까지 타는 구간의 예상 혼잡. to_seq가 없으면 from_seq 한 구간이다.

    at은 그 구간을 타는 시각(시간대가 있는 값)이며 한국 시각의 요일유형·시간대로 행을 고른다.
    """
    if policy is None:
        return unavailable(NOT_CONFIGURED)
    if not profiles:
        return unavailable(PROFILE_MISSING, policy)
    if at is None or at.tzinfo is None:
        return unavailable(TIME_UNKNOWN, policy)
    local = at.astimezone(KST)
    kind, hour = day_type(local.date(), policy.holidays), local.hour
    seqs = range(from_seq, max(from_seq + 1, to_seq if to_seq is not None else from_seq + 1))

    fallback = None
    rows = _rows_for(profiles, kind, hour, seqs)
    candidates: list[tuple[float, str, int, Mapping[str, Any]]] = []
    if rows:
        candidates = [(*found, seq, row) for seq, row in rows if (found := _load(row)) is not None]
    else:
        # ① 같은 요일유형의 앞뒤 시간대 평균.
        near = [_rows_for(profiles, kind, h, seqs) for h in (hour - 1, hour + 1) if 0 <= h <= 23]
        near = [group for group in near if group]
        if near:
            fallback = "adjacent_hours"
            for seq in seqs:
                found = [(loaded, row) for group in near for s, row in group
                         if s == seq and (loaded := _load(row)) is not None]
                if found:
                    load = sum(loaded[0] for loaded, _ in found) / len(found)
                    basis = "p80" if all(loaded[1] == "p80" for loaded, _ in found) else "mean"
                    candidates.append((load, basis, seq, min((row for _, row in found),
                                                             key=lambda row: row["sample_days"])))
            rows = [pair for group in near for pair in group]
        elif kind != "weekday":
            # ② 평일의 같은 시간대.
            rows = _rows_for(profiles, "weekday", hour, seqs)
            if rows:
                fallback = "weekday"
                candidates = [(*found, seq, row) for seq, row in rows if (found := _load(row)) is not None]
    if not rows:
        return unavailable(TIME_MISSING, policy)
    if not candidates:
        return unavailable(TRIPS_UNKNOWN, policy)
    load, basis, seq, row = max(candidates, key=lambda item: (item[0], -item[2]))
    index = level_of(load, policy)
    return {
        "status": "available", "level": LEVELS[index - 1], "level_index": index, "load_per_vehicle": round(load, 1),
        "basis": basis, "stop_seq": seq, "day_type": kind, "hour": hour, "sample_days": row["sample_days"],
        "source_period": row["source_period"], "trips_per_hour": row["trips_per_hour"],
        "trips_source": row["trips_source"],
        "low_confidence": policy.min_sample_days is not None and row["sample_days"] < policy.min_sample_days,
        "fallback": fallback, "seats": policy.seats, "capacity": policy.capacity,
        "policy_version": policy.version, "reason": None,
    }


def view(congestion: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """저장 형식(snake_case)을 API 형식(camelCase)으로 옮긴다. '평소 이 시간대 예상'이며 실시간 값이 아니다."""
    if not congestion:
        return None
    out: dict[str, Any] = {"kind": "typical_for_time"}
    for key, value in congestion.items():
        head, *rest = key.split("_")
        out[head + "".join(part[:1].upper() + part[1:] for part in rest)] = value
    return out
