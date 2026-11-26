"""대전시 노선 위치 기록에서 운행을 나누고 정류소 도착 이벤트를 도출하는 규칙(순수 함수, DB 접근 없음).

위치 기록은 노선 위치 조회(getBusPosByRtid)에서 차량의 정류소(BUS_NODE_ID)가 바뀐 때만 저장한 행이다
(transit.service.normalize_daejeon_positions). 저장할 때 운행 키(run_group)를 정해 함께 남기므로, 나중에 다시 도출해도
같은 운행·같은 이벤트 키가 나온다.

운행 분리(next_run_group)
- 경유 순번이 줄거나, 직전 저장 기록과의 수집 간격이 설정한 값(BACKEND_ETA_ERROR_RUN_GAP_SECONDS)을 넘으면 새 운행이다.
- 정류소가 그대로인 기록은 저장하지 않는다(회차지에 서 있는 차량이 행을 늘리지 않게 한다).

도착 이벤트(derive_arrivals): 한 운행의 기록을 수집 순으로 놓고 대상 경유 순번마다
- 대상 정류소 기록이 있고 ARR_TIME의 뜻이 arrival이면 그 ARR_TIME을 점 시각(exact)으로 쓴다.
- 뜻이 departure이면 [직전 기록의 ARR_TIME, 대상 정류소 ARR_TIME] 범위(bounded)로 쓴다.
- 뜻이 unverified이거나 대상 정류소 기록을 놓쳤으면 [직전 기록의 ARR_TIME, 다음 기록의 ARR_TIME] 범위(bounded)로 쓴다.
- 필요한 앞뒤 기록이 없으면 이벤트를 만들지 않는다.
직전·다음 기록은 같은 운행에서 대상보다 앞·뒤 순번인 가장 가까운 기록이다(인접 정류소 기록을 놓쳤으면 그만큼 범위가 넓다).
범위로만 아는 도착을 중간값 같은 대표 시각으로 바꾸지 않고, 수집 시각을 도착 시각으로 쓰지 않는다.
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
# 이벤트 시각의 근거.
BASIS_TARGET_ARR_TIME = "target_arr_time"
BASIS_PREVIOUS_TO_TARGET = "previous_to_target"
BASIS_PREVIOUS_TO_NEXT = "previous_to_next"


@dataclass(frozen=True)
class VehicleState:
    """한 차량의 마지막 저장 위치 기록(같은 노선). 다음 기록을 저장할지, 어느 운행에 넣을지 정하는 데 쓴다."""

    run_group: str
    node_id: str
    # 그 운행에서 마지막으로 확인된 경유 순번(순번을 정하지 못한 기록은 건너뛴다).
    last_seq: int | None
    collected_at: datetime


def run_group_key(route_external_id: str, vehicle_no: str, started_at: datetime) -> str:
    """운행 키: 노선·차량번호·그 운행의 첫 저장 기록 수집 시각(UTC)."""
    return f"dj:{route_external_id}:{vehicle_no}:{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def next_run_group(state: VehicleState | None, *, route_external_id: str, vehicle_no: str, node_id: str,
                   stop_seq: int | None, collected_at: datetime, run_gap_seconds: float) -> tuple[str | None, bool]:
    """(이번 기록의 운행 키, 새 운행 여부). 정류소가 직전 저장 기록과 같으면 저장하지 않으므로 (None, False)다."""
    if state is not None and state.node_id == node_id:
        return None, False
    continues = (
        state is not None
        and (collected_at - state.collected_at).total_seconds() <= run_gap_seconds
        and not (stop_seq is not None and state.last_seq is not None and stop_seq < state.last_seq)
    )
    if continues:
        return state.run_group, False
    return run_group_key(route_external_id, vehicle_no, collected_at), True


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
