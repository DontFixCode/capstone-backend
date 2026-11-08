"""주기 작업의 실행 시점 계산. 수집 이력은 DB(api_fetches)에서 다시 읽으므로 재시작·다중 실행에도 중복 호출하지 않는다.

실시간 대상
- 공개값도 시도 기록도 없으면 즉시 최초 조회한다.
- 대상마다 키에서 정한 고정 위상(phase)의 슬롯(phase + k × 간격)에 조회해 한 주기 안의 요청을 분산한다.
- 워커가 직전 슬롯에 직접 조회했고 그 뒤 다른 시도가 없으면 다음 슬롯(직전 슬롯 + 간격)에 조회한다.
- 그 밖의 경우(최초 조회 직후·재시작·다른 프로세스의 조회)는 마지막 시도·공개 수집 시각에서 간격 이상 지난 첫 슬롯을
  쓴다. 그래서 간격보다 일찍 다시 조회하지 않으며, 위상에 맞출 때 한 번만 간격이 최대 두 배까지 늘어난다.
정적 정보: 마지막 정적 시도에서 갱신 간격(하루)이 지나면 다시 적재한다.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from app.modules.transit import repository
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import realtime_target_key

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def phase_offset(key: str, interval_seconds: float) -> float:
    """대상 키에서 정한 [0, interval) 범위의 고정 위상. 다른 대상이 추가·해제돼도 바뀌지 않는다."""
    digest = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
    return (digest % int(interval_seconds * 1000)) / 1000.0


def first_slot_at_or_after(key: str, interval_seconds: float, moment: datetime) -> datetime:
    phase = phase_offset(key, interval_seconds)
    elapsed = (moment - _EPOCH).total_seconds() - phase
    return _EPOCH + timedelta(seconds=math.ceil(elapsed / interval_seconds) * interval_seconds + phase)


@dataclass(frozen=True)
class Due:
    at: datetime
    # True면 위상 슬롯이다. 최초 조회(즉시)는 슬롯이 아니므로 다음 슬롯 계산에 쓰지 않는다.
    on_slot: bool
    reason: str


def next_due(key: str, interval_seconds: float, last_attempt_at: datetime | None,
             last_collected_at: datetime | None, now: datetime,
             *, ran_slot: tuple[datetime, datetime] | None = None) -> Due:
    """ran_slot은 이 워커가 마지막으로 조회한 (슬롯 시각, 조회 종료 시각)이다(프로세스 메모리).

    DB의 마지막 시도가 그 조회 구간 안에 있을 때만 다음 슬롯을 쓰고, 그 뒤에 다른 시도가 있었으면 간격 규칙을 다시 적용한다.
    """
    known = [t for t in (last_attempt_at, last_collected_at) if t is not None]
    if not known:
        return Due(now, False, "initial")
    interval = timedelta(seconds=interval_seconds)
    if ran_slot is not None and last_attempt_at is not None and ran_slot[0] <= last_attempt_at <= ran_slot[1]:
        return Due(ran_slot[0] + interval, True, "next_slot")
    return Due(first_slot_at_or_after(key, interval_seconds, max(known) + interval), True, "aligned_after_interval")


@dataclass(frozen=True)
class PlannedTarget:
    target: RealtimeTarget
    target_key: str
    due: Due
    last_attempt_at: datetime | None
    last_collected_at: datetime | None


def plan_realtime(conn: psycopg.Connection, targets: list[RealtimeTarget], interval_seconds: float, now: datetime,
                  ran_slots: dict[str, tuple[datetime, datetime]] | None = None) -> list[PlannedTarget]:
    ran_slots = ran_slots or {}
    keyed = [(t, realtime_target_key(t)) for t in targets]
    keys_by_kind: dict[str, list[str]] = {}
    for target, key in keyed:
        keys_by_kind.setdefault(target.kind, []).append(key)
    timings: dict[str, tuple[datetime | None, datetime | None]] = {}
    for kind, keys in keys_by_kind.items():
        timings.update(repository.target_timings(conn, kind, keys))
    plans = []
    for target, key in keyed:
        last_attempt, last_collected = timings.get(key, (None, None))
        due = next_due(key, interval_seconds, last_attempt, last_collected, now, ran_slot=ran_slots.get(key))
        plans.append(PlannedTarget(target, key, due, last_attempt, last_collected))
    return sorted(plans, key=lambda p: (p.due.at, p.target_key))


def static_due(conn: psycopg.Connection, refresh_interval_seconds: float, now: datetime) -> tuple[bool, datetime]:
    last_attempt = repository.static_fetch_status(conn)["last_attempt_at"]
    if last_attempt is None:
        return True, now
    due_at = last_attempt + timedelta(seconds=refresh_interval_seconds)
    return due_at <= now, due_at
