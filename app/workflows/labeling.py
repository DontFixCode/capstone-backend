"""환승 표본·공동 관측 묶음의 사후 판정 흐름(워커가 주기마다 호출).

transit의 도출 도착 이벤트(transit_observations kind=event)와 evaluation의 표본·묶음을 잇는다. 예측 값은 바꾸지 않고
관측 연결·판정 컬럼만 갱신한다. 같은 패턴·경유 위치·운행 회차의 도착 점 이벤트만 사용하며, 범위 이벤트는 범위로
보존하고 판정 기한이 지나도 점 시각이 없으면 insufficient로 둔다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from app.modules.evaluation import repository as evaluation_repository
from app.modules.evaluation.samples import EventMatch, label_sample, label_scene
from app.modules.transit import repository as transit_repository


def _match(conn: psycopg.Connection, ctx: dict[str, Any]) -> EventMatch | None:
    if not ctx.get("run_key") or ctx.get("stop_order") is None:
        return None
    event = transit_repository.find_arrival_event(conn, ctx["pattern_id"], ctx["stop_order"], ctx["run_key"])
    if event is None:
        return None
    details = event["details"] or {}
    lower, upper = details.get("bounded_lower_at"), details.get("bounded_upper_at")
    return EventMatch(event["id"], event["event_at"], event["quality"],
                      datetime.fromisoformat(lower) if lower else None,
                      datetime.fromisoformat(upper) if upper else None)


def label_due(conn: psycopg.Connection, now: datetime, *, limit: int = 200) -> dict[str, int]:
    """판정 대기 표본·묶음을 처리한다. 호출자가 트랜잭션을 커밋한다."""
    counts = {"samples_labeled": 0, "samples_insufficient": 0, "scenes_complete": 0, "scenes_insufficient": 0,
              "still_pending": 0}
    for row in evaluation_repository.pending_samples(conn, limit):
        ctx = row["prediction_context"]
        label = label_sample(row, _match(conn, ctx["front"]), _match(conn, ctx["next"]), now)
        if label is None:
            counts["still_pending"] += 1
            continue
        evaluation_repository.apply_sample_label(conn, row["id"], label, now)
        counts[f"samples_{label.label_status}"] += 1
    for row in evaluation_repository.pending_scenes(conn, limit):
        matches = {}
        for p in row["predictions"]:
            for side in ("board", "alight"):
                matches[(p["ride_index"], p["vehicle_rank"], side)] = _match(conn, p[side])
        result = label_scene(row, matches, now)
        if result is None:
            counts["still_pending"] += 1
            continue
        status, observations, quality = result
        evaluation_repository.apply_scene_label(conn, row["id"], status, observations, quality, now)
        counts[f"scenes_{status}"] += 1
    return counts
