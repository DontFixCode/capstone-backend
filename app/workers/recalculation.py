"""활성 이동 세션의 안내 재계산 실행(1분 주기 + 수동 입력 즉시 예약).

- next_recalc_at이 지난 활성 이동을 FOR UPDATE SKIP LOCKED로 가져가 예약을 임대 만료 시각으로 미루고 바로 커밋한다.
  계산 중에는 행 잠금을 유지하지 않는다.
- 결과는 계산에 쓴 state_version이 그대로이고 활성·탈퇴 전일 때만 guidance·result_version·다음 예약으로 저장한다.
  조건이 맞지 않으면 결과를 버리고 새 입력이 만든 예약을 유지한다(오래된 계산이 예약을 지우거나 늦추지 않음).
- 앱 연결 여부(connection_state)는 대상 선정에 쓰지 않는다. 백그라운드·연결 단절에도 계속 계산한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.db.session import Database
from app.modules.journeys import repository as journey_repository
from app.workflows import guidance as guidance_flow
from app.workflows import search as search_flow

logger = logging.getLogger(__name__)


def process_due_journeys(
    db: Database,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    network_cache: guidance_flow.NetworkCache | None = None,
    realtime_provider: search_flow.RealtimeProvider | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    cache = network_cache or guidance_flow.NetworkCache()
    interval = timedelta(seconds=settings.journey_recalc_interval_seconds)
    with db.transaction() as conn:
        claims = journey_repository.claim_due_journeys(conn, clock(), lease_seconds=settings.journey_recalc_lease_seconds,
                                                       limit=limit)
    results: list[dict[str, Any]] = []
    for claim in claims:
        journey_id = claim["id"]
        with db.transaction() as conn:
            journey = journey_repository.get_journey(conn, journey_id)
        if journey is None or journey["lifecycle_status"] != "active":
            continue
        try:
            guidance = guidance_flow.compute_guidance(db, journey, settings, now=clock(), network_cache=cache,
                                                      realtime_provider=realtime_provider)
        except Exception as exc:
            logger.exception("이동 %s 안내 계산 실패", journey_id)
            with db.transaction() as conn:
                journey_repository.release_stale_lease(conn, journey_id, leased_until=claim["leased_until"],
                                                       next_recalc_at=clock() + interval)
            results.append({"journey_id": str(journey_id), "action": "failed", "error": type(exc).__name__})
            continue
        with db.transaction() as conn:
            version = journey_repository.save_guidance(
                conn, journey_id, expected_state_version=journey["state_version"],
                leased_until=claim["leased_until"], guidance=search_flow._jsonable(guidance),
                next_recalc_at=clock() + interval)
            if version is None:
                # 상태가 바뀌었으면 새 입력이 이미 즉시 재평가를 예약했으므로 아무것도 바꾸지 않는다(임대 값이 아님).
                journey_repository.release_stale_lease(conn, journey_id, leased_until=claim["leased_until"],
                                                       next_recalc_at=clock() + interval)
        action = "saved" if version is not None else "discarded_stale_state"
        results.append({"journey_id": str(journey_id), "action": action, "result_version": version,
                        "base_state_version": journey["state_version"], "status": guidance.get("status")})
        logger.info("이동 %s 안내 %s (결과 버전 %s, %s)", journey_id, action, version, guidance.get("status"))
    return results
