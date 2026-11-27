"""활성 이동 세션의 안내 재계산 실행(1분 주기 + 수동 입력 즉시 예약).

- next_recalc_at이 지난 활성 이동을 FOR UPDATE SKIP LOCKED로 가져가 예약을 임대 만료 시각으로 미루고 바로 커밋한다.
  계산 중에는 행 잠금을 유지하지 않는다.
- 계산 직전에 지금 안내에 바로 쓰이는 도착정보(탑승 전: 다음 승차 정류소, 탑승 중: 하차 정류소)를 새로 받는다. 공유 수집은
  대상마다 수집 간격(기본 10분)에 한 번이라, 그 값만 쓰면 이미 지나간 차량이 남고 버스를 놓친 뒤 다음 차량이 보이지 않는다.
  BACKEND_JOURNEY_REALTIME_REFRESH_SECONDS 이내에 받은 값은 다시 받지 않고, 상태가 바뀐 뒤 첫 계산은 항상 새로 받는다
  (이동 1건·재계산 1회에 대전시 도착정보 최대 1건). 조회 실패는 계산을 막지 않는다(마지막 공개값으로 계산).
  사용자 입력·위치·스트림 연결이 BACKEND_JOURNEY_REALTIME_REFRESH_IDLE_SECONDS 넘게 없으면 주기 갱신을 멈추고 공유 수집
  값만 쓴다(끝내지 않고 방치한 이동이 하루 1,440건씩 호출하지 않게 한다). 새 입력이 오면 다시 받는다.
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
from app.integrations.daejeon_bus_api import DaejeonBusClient, DaejeonBusConfigError
from app.modules.journeys import repository as journey_repository
from app.workflows import guidance as guidance_flow
from app.workflows import search as search_flow

logger = logging.getLogger(__name__)


def last_activity_at(journey: dict[str, Any]) -> datetime | None:
    """이 이동에 사용자·앱의 입력이 마지막으로 닿은 시각: 시작·경로 변경·탑승·하차·버스 놓침·위치 수신·스트림 연결."""
    progress = journey.get("current_progress") or {}
    snapshot = journey.get("selected_snapshot") or {}
    recorded = [progress.get("boarded"), progress.get("last_alight"), *(progress.get("missed_vehicles") or [])]
    times = [journey.get("started_at"), journey.get("location_received_at"), journey.get("last_connected_at"),
             guidance_flow._parse_time(snapshot.get("selected_at")),
             *(guidance_flow._parse_time(item.get("received_at")) for item in recorded if isinstance(item, dict))]
    known = [at for at in times if at is not None]
    return max(known) if known else None


def refresh_focus_targets(db: Database, journey: dict[str, Any], settings: Settings, daejeon_client: DaejeonBusClient,
                          *, clock: Callable[[], datetime]) -> dict[str, dict[str, Any]]:
    """이 이동의 안내에 바로 쓰이는 도착정보를 새로 받는다. 대상별 처리 결과(수집기 형식)를 돌려준다.

    상태가 바뀐 뒤 첫 계산(저장된 안내의 기준 상태 버전이 현재와 다름)은 받은 지 얼마 안 된 값이 있어도 다시 받는다.
    탑승·하차·버스 놓침 직후에는 직전 응답에 방금 지나간 차량이 남아 있기 때문이다. 같은 상태의 주기 재계산은 최근 입력이
    있는 이동만 새로 받는다(방치된 이동은 공유 수집 값으로 계산).
    """
    targets = guidance_flow.focus_targets(journey, f"{guidance_flow.JOURNEY_SOURCE}:{journey['id']}")
    if not targets:
        return {}
    state_changed = (journey.get("guidance") or {}).get("base_state_version") != journey["state_version"]
    if not state_changed:
        active_at = last_activity_at(journey)
        idle = settings.journey_realtime_refresh_idle_seconds
        if active_at is None or (clock() - active_at).total_seconds() > idle:
            return {}
    return search_flow.collect_targets(db, None, targets, settings.journey_realtime_refresh_seconds, clock,
                                       daejeon_client=daejeon_client, force=state_changed)


def process_due_journeys(
    db: Database,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    network_cache: guidance_flow.NetworkCache | None = None,
    realtime_provider: search_flow.RealtimeProvider | None = None,
    limit: int = 20,
    daejeon_client_factory: Callable[[Settings], DaejeonBusClient | None] | None = None,
) -> list[dict[str, Any]]:
    cache = network_cache or guidance_flow.NetworkCache()
    interval = timedelta(seconds=settings.journey_recalc_interval_seconds)
    with db.transaction() as conn:
        claims = journey_repository.claim_due_journeys(conn, clock(), lease_seconds=settings.journey_recalc_lease_seconds,
                                                       limit=limit)
    results: list[dict[str, Any]] = []
    # 합성 실시간 입력(격리 시험)을 쓰거나 대전시 API가 설정되지 않았으면 새로 받지 않는다.
    daejeon = None
    if claims and realtime_provider is None and daejeon_client_factory is not None:
        try:
            daejeon = daejeon_client_factory(settings)
        except DaejeonBusConfigError as exc:
            logger.warning("대전시 버스 API 미설정(이동 안내용 도착정보를 새로 받지 않음): %s", exc)
    try:
        for claim in claims:
            journey_id = claim["id"]
            with db.transaction() as conn:
                journey = journey_repository.get_journey(conn, journey_id)
            if journey is None or journey["lifecycle_status"] != "active":
                continue
            refreshed: dict[str, dict[str, Any]] = {}
            if daejeon is not None:
                try:
                    refreshed = refresh_focus_targets(db, journey, settings, daejeon, clock=clock)
                except Exception:  # 조회 실패가 안내 계산을 막지 않게 한다(마지막 공개값으로 계산).
                    logger.exception("이동 %s 안내용 도착정보 조회 실패", journey_id)
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
                            "base_state_version": journey["state_version"], "status": guidance.get("status"),
                            "refreshed": {key: outcome.get("action") for key, outcome in refreshed.items()}})
            logger.info("이동 %s 안내 %s (결과 버전 %s, %s)", journey_id, action, version, guidance.get("status"))
    finally:
        if daejeon is not None:
            daejeon.close()
    return results
