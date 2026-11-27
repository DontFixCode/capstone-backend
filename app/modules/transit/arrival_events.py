"""노선 위치 기록에서 운행을 나누고 정류소 도착 이벤트를 도출하는 규칙(순수 함수, DB 접근 없음).

위치 기록의 원천은 둘이고(BACKEND_ETA_ERROR_POSITION_SOURCE), 저장할 때 운행 키(run_group)를 정해 함께 남기므로 나중에
다시 도출해도 같은 운행·같은 이벤트 키가 나온다.

운행 분리(공통)
- 경유 순번이 줄거나, 정류소의 방향(route_stops.direction_code)이 바뀌거나, 직전 저장 기록과의 수집 간격이 설정한 값
  (BACKEND_ETA_ERROR_RUN_GAP_SECONDS)을 넘으면 새 운행이다. 한 방향의 운행 하나가 운행 1회다.

1) 대전시 노선 위치(getBusPosByRtid, transit.service.normalize_daejeon_positions)
- 차량의 정류소(BUS_NODE_ID)가 바뀐 때만 저장한다(next_run_group). 정류소가 그대로인 기록은 저장하지 않는다.
- 도착 이벤트(derive_arrivals): 한 운행의 기록을 수집 순으로 놓고 대상 경유 순번마다
  - 대상 정류소 기록이 있고 ARR_TIME의 뜻이 arrival이면 그 ARR_TIME을 점 시각(exact)으로 쓴다.
  - 뜻이 departure이면 [직전 기록의 ARR_TIME, 대상 정류소 ARR_TIME] 범위(bounded)로 쓴다.
  - 뜻이 unverified이거나 대상 정류소 기록을 놓쳤으면 [직전 기록의 ARR_TIME, 다음 기록의 ARR_TIME] 범위(bounded)로 쓴다.
  - 필요한 앞뒤 기록이 없으면 이벤트를 만들지 않는다.
  직전·다음 기록은 같은 운행에서 대상보다 앞·뒤 순번인 가장 가까운 기록이다(인접 정류소 기록을 놓쳤으면 그만큼 범위가 넓다).
  수집 시각을 도착 시각으로 쓰지 않는다.

2) TAGO 노선 위치(getRouteAcctoBusLcList, transit.service.normalize_positions)
- 조회할 때마다 응답의 모든 차량을 저장한다(snapshot_run_group). 원천이 관측 시각을 주지 않는다.
- 도착 이벤트(derive_snapshot_arrivals): 한 운행의 기록을 수집 순으로 놓고 대상 경유 순번마다, 차량이 처음으로 대상
  순번 이상에서 확인된 조회와 그 직전 조회(대상보다 앞 순번)를 찾는다. 도착은 (직전 조회를 보낸 시각, 이번 응답을 받은
  시각] 범위(bounded)다. 조회 시각은 범위의 양 끝으로만 쓰고 도착 시각 자체로 쓰지 않는다.
- 직전 조회가 없으면(처음 확인됐을 때 이미 대상 이상) 이벤트를 만들지 않는다.
- 하한은 원천이 실제 위치를 지연 없이 반영한다고 본 값이다. 지연이 있으면 실제 도착은 하한보다 그만큼 이를 수 있다(확인 전).

범위로만 아는 도착을 중간값 같은 대표 시각으로 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ARR_TIME_UNVERIFIED = "unverified"
ARR_TIME_ARRIVAL = "arrival"
ARR_TIME_DEPARTURE = "departure"
ARR_TIME_MEANINGS = (ARR_TIME_UNVERIFIED, ARR_TIME_ARRIVAL, ARR_TIME_DEPARTURE)
RULE_VERSION = "daejeon-position-arrival/1"
SNAPSHOT_RULE_VERSION = "tago-position-arrival/1"
# 위치 기록의 원천(BACKEND_ETA_ERROR_POSITION_SOURCE).
SOURCE_DAEJEON = "daejeon"
SOURCE_TAGO = "tago"
POSITION_SOURCES = (SOURCE_DAEJEON, SOURCE_TAGO)
# 운행 키의 원천 접두사.
RUN_PREFIX_DAEJEON = "dj"
RUN_PREFIX_TAGO = "tago"
# 이벤트 시각의 근거.
BASIS_TARGET_ARR_TIME = "target_arr_time"
BASIS_PREVIOUS_TO_TARGET = "previous_to_target"
BASIS_PREVIOUS_TO_NEXT = "previous_to_next"
BASIS_SNAPSHOT_PAIR = "snapshot_before_to_snapshot_at_or_after"


@dataclass(frozen=True)
class VehicleState:
    """한 차량의 마지막 저장 위치 기록(같은 노선). 다음 기록을 저장할지, 어느 운행에 넣을지 정하는 데 쓴다."""

    run_group: str
    # 대전시 기록의 정류소(BUS_NODE_ID). 조회마다 모두 저장하는 TAGO 기록에서는 쓰지 않는다.
    node_id: str
    # 그 운행에서 마지막으로 확인된 경유 순번(순번을 정하지 못한 기록은 건너뛴다).
    last_seq: int | None
    collected_at: datetime
    # 그 운행에서 마지막으로 확인된 정류소 방향(route_stops.direction_code). 모르면 None이다.
    direction: str | None = None


def run_group_key(route_external_id: str, vehicle_no: str, started_at: datetime,
                  prefix: str = RUN_PREFIX_DAEJEON) -> str:
    """운행 키: 원천·노선·차량번호·그 운행의 첫 저장 기록 수집 시각(UTC)."""
    return f"{prefix}:{route_external_id}:{vehicle_no}:{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def _continues(state: VehicleState | None, *, stop_seq: int | None, direction: str | None, collected_at: datetime,
               run_gap_seconds: float) -> bool:
    """직전 저장 기록과 같은 운행인지. 간격 초과·순번 감소·방향 변경이면 새 운행이다(모르는 값은 판단에 쓰지 않는다)."""
    return (
        state is not None
        and (collected_at - state.collected_at).total_seconds() <= run_gap_seconds
        and not (stop_seq is not None and state.last_seq is not None and stop_seq < state.last_seq)
        and not (direction is not None and state.direction is not None and direction != state.direction)
    )


def next_run_group(state: VehicleState | None, *, route_external_id: str, vehicle_no: str, node_id: str,
                   stop_seq: int | None, collected_at: datetime, run_gap_seconds: float,
                   direction: str | None = None) -> tuple[str | None, bool]:
    """(이번 기록의 운행 키, 새 운행 여부). 정류소가 직전 저장 기록과 같으면 저장하지 않으므로 (None, False)다."""
    if state is not None and state.node_id == node_id:
        return None, False
    if _continues(state, stop_seq=stop_seq, direction=direction, collected_at=collected_at,
                  run_gap_seconds=run_gap_seconds):
        return state.run_group, False
    return run_group_key(route_external_id, vehicle_no, collected_at), True


def snapshot_run_group(state: VehicleState | None, *, route_external_id: str, vehicle_no: str,
                       stop_seq: int | None, direction: str | None, collected_at: datetime,
                       run_gap_seconds: float) -> tuple[str, bool]:
    """조회마다 모든 차량을 저장하는 위치 기록(TAGO)의 (운행 키, 새 운행 여부). 정류소가 그대로여도 기록은 남는다."""
    if _continues(state, stop_seq=stop_seq, direction=direction, collected_at=collected_at,
                  run_gap_seconds=run_gap_seconds):
        return state.run_group, False
    return run_group_key(route_external_id, vehicle_no, collected_at, RUN_PREFIX_TAGO), True


def target_positions(visits: Sequence[Mapping[str, Any]],
                     stop_external_ids: Iterable[str]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    """노선 경유 목록에서 대상 정류소의 경유 위치를 찾는다. (정류소 원천 ID → 경유 행, 제외한 정류소 → 사유).

    한 번만 지나는 정류소만 쓴다. 두 번 이상 지나면 위치 기록이 어느 경유인지 정할 수 없어 제외한다.
    """
    found: dict[str, Mapping[str, Any]] = {}
    excluded: dict[str, str] = {}
    for stop_id in stop_external_ids:
        hits = [visit for visit in visits if visit["stop_external_id"] == stop_id]
        if len(hits) == 1:
            found[stop_id] = hits[0]
        else:
            excluded[stop_id] = "repeated_visit" if hits else "stop_not_on_route"
    return found, excluded


@dataclass(frozen=True)
class RunPosition:
    """저장된 위치 기록 1건. arr_time_at은 원천 ARR_TIME(KST)을 해석한 시각이며 형식이 다르면 None이다."""

    observation_id: int
    run_group: str
    stop_seq: int | None
    arr_time_at: datetime | None
    collected_at: datetime


@dataclass(frozen=True)
class DerivedArrival:
    """도출한 도착 이벤트. 점 시각이면 event_at = lower_at = upper_at이고, 범위면 event_at은 None이다."""

    event_key: str
    run_group: str
    stop_seq: int
    quality: str
    event_at: datetime | None
    lower_at: datetime
    upper_at: datetime
    basis: str
    lower_stop_seq: int
    upper_stop_seq: int
    source_observation_ids: tuple[int, ...]
    # 근거로 쓴 기록 가운데 가장 늦은 수집 시각(이벤트 행의 collected_at). 도착 시각이 아니다.
    collected_at: datetime


def event_key(run_group: str, stop_seq: int) -> str:
    """운행 하나가 경유 위치 하나를 지난 사건의 키. ARR_TIME 해석 설정을 넣지 않아 한 통과에 이벤트는 하나다."""
    return f"daejeon-position-arrival:{run_group}:{stop_seq}"


def derive_arrivals(positions: Iterable[RunPosition], target_seqs: Collection[int], meaning: str) -> list[DerivedArrival]:
    """위치 기록(여러 운행이 섞여 있어도 된다)에서 대상 경유 순번의 도착 이벤트를 만든다."""
    if meaning not in ARR_TIME_MEANINGS:
        raise ValueError(f"ARR_TIME의 뜻은 {ARR_TIME_MEANINGS} 중 하나여야 합니다.")
    runs: dict[str, list[RunPosition]] = {}
    for position in positions:
        # 순번이나 시각을 알 수 없는 기록은 앞뒤 근거로 쓰지 못한다.
        if position.stop_seq is not None and position.arr_time_at is not None:
            runs.setdefault(position.run_group, []).append(position)
    derived: list[DerivedArrival] = []
    for run_group, items in sorted(runs.items()):
        items.sort(key=lambda item: (item.collected_at, item.observation_id))
        for seq in sorted(set(target_seqs)):
            arrival = _derive_one(run_group, items, seq, meaning)
            if arrival is not None:
                derived.append(arrival)
    return derived


def _derive_one(run_group: str, items: Sequence[RunPosition], seq: int, meaning: str) -> DerivedArrival | None:
    target = next((item for item in items if item.stop_seq == seq), None)
    before = [item for item in items if item.stop_seq < seq]
    after = [item for item in items if item.stop_seq > seq]
    previous = max(before, key=lambda item: (item.stop_seq, item.collected_at)) if before else None
    following = min(after, key=lambda item: (item.stop_seq, item.collected_at)) if after else None

    def arrival(quality: str, basis: str, lower: RunPosition, upper: RunPosition) -> DerivedArrival | None:
        if lower.arr_time_at > upper.arr_time_at:
            # 앞 기록의 시각이 뒤 기록보다 늦으면 같은 운행의 기록으로 볼 수 없다.
            return None
        used = tuple(dict.fromkeys(item.observation_id for item in (lower, target, upper) if item is not None))
        return DerivedArrival(
            event_key=event_key(run_group, seq), run_group=run_group, stop_seq=seq, quality=quality,
            event_at=lower.arr_time_at if quality == "exact" else None,
            lower_at=lower.arr_time_at, upper_at=upper.arr_time_at, basis=basis,
            lower_stop_seq=lower.stop_seq, upper_stop_seq=upper.stop_seq, source_observation_ids=used,
            collected_at=max(item.collected_at for item in (lower, target, upper) if item is not None))

    if target is not None and meaning == ARR_TIME_ARRIVAL:
        return arrival("exact", BASIS_TARGET_ARR_TIME, target, target)
    if target is not None and meaning == ARR_TIME_DEPARTURE:
        return None if previous is None else arrival("bounded", BASIS_PREVIOUS_TO_TARGET, previous, target)
    if previous is None or following is None:
        return None
    return arrival("bounded", BASIS_PREVIOUS_TO_NEXT, previous, following)


# ---------------------------------------------------------------- 조회 시점마다 저장한 위치 기록(TAGO)

@dataclass(frozen=True)
class SnapshotPosition:
    """조회할 때마다 저장한 위치 기록 1건(TAGO 노선 위치). 원천 관측 시각이 없어 조회를 보낸 시각과 응답을 받은 시각만 안다."""

    observation_id: int
    run_group: str
    stop_seq: int | None
    # 이 기록이 들어 있던 조회를 보낸 시각. 모르면 그 기록은 하한 근거로 쓰지 못한다.
    requested_at: datetime | None
    # 응답을 받은 시각(관측 행의 collected_at).
    received_at: datetime


def snapshot_event_key(run_group: str, stop_seq: int) -> str:
    """운행 하나가 경유 위치 하나를 지난 사건의 키(TAGO 위치 기록에서 도출)."""
    return f"tago-position-arrival:{run_group}:{stop_seq}"


def derive_snapshot_arrivals(positions: Iterable[SnapshotPosition],
                             target_seqs: Collection[int]) -> list[DerivedArrival]:
    """조회 시점별 위치 기록(여러 운행이 섞여 있어도 된다)에서 대상 경유 순번의 도착 이벤트(범위)를 만든다.

    차량이 처음으로 대상 순번 이상에서 확인된 조회와 그 직전 조회 사이에 도착했다고 본다. 직전 조회에서 차량은 대상보다 앞
    순번에 있었으므로 도착은 그 조회를 보낸 시각 뒤이고, 이번 응답에 이미 대상 이상이므로 응답을 받은 시각 이전이다.
    """
    runs: dict[str, list[SnapshotPosition]] = {}
    for position in positions:
        # 순번을 알 수 없는 기록은 앞뒤 근거로 쓰지 못한다.
        if position.stop_seq is not None:
            runs.setdefault(position.run_group, []).append(position)
    derived: list[DerivedArrival] = []
    for run_group, items in sorted(runs.items()):
        items.sort(key=lambda item: (item.received_at, item.observation_id))
        for seq in sorted(set(target_seqs)):
            index = next((i for i, item in enumerate(items) if item.stop_seq >= seq), None)
            if index is None or index == 0:
                # 아직 대상에 닿지 않았거나, 처음 확인됐을 때 이미 대상 이상이라 하한을 알 수 없다.
                continue
            before, at = items[index - 1], items[index]
            if before.requested_at is None or before.requested_at > at.received_at:
                continue
            derived.append(DerivedArrival(
                event_key=snapshot_event_key(run_group, seq), run_group=run_group, stop_seq=seq, quality="bounded",
                event_at=None, lower_at=before.requested_at, upper_at=at.received_at, basis=BASIS_SNAPSHOT_PAIR,
                lower_stop_seq=before.stop_seq, upper_stop_seq=at.stop_seq,
                source_observation_ids=(before.observation_id, at.observation_id), collected_at=at.received_at))
    return derived
