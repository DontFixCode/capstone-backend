"""앱이 접수한 최초 검색(processing)을 워커가 실행한다.

- 접수와 계산을 분리한다: API는 검색 행만 만들고, 이 작업이 행을 임대(summary.job.lease_until)해 실행한다.
- 임대는 짧은 트랜잭션으로 잡고 바로 커밋한다. 외부 호출(TMAP·TAGO) 동안 검색 행 잠금을 유지하지 않는다.
- 워커가 중단돼 임대가 지나면 다시 가져가고, 시도 수를 다 쓰거나 유효기간이 지난 검색은 error로 끝내 processing에 남지 않는다.
- 같은 워커 프로세스의 공유 수집과 검색 시점 수집은 대상별 DB 잠금을 기다리지 않는 방식(try-lock)이라 서로 막히지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.db.session import Database
from app.integrations.bus_api import TagoClient, TagoConfigError
from app.integrations.daejeon_bus_api import DaejeonBusClient, DaejeonBusConfigError
from app.integrations.walking_api import TmapConfigError, TmapWalkingClient
from app.modules.routing import repository as search_repository
from app.workflows import search as search_flow

logger = logging.getLogger(__name__)


def process_search_jobs(
    db: Database,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    tago_client_factory: Callable[[Settings], TagoClient | None] | None = None,
    walking_client_factory: Callable[[Settings], TmapWalkingClient | None] | None = None,
    realtime_provider: search_flow.RealtimeProvider | None = None,
    limit: int = 5,
    daejeon_client_factory: Callable[[Settings], DaejeonBusClient | None] | None = None,
) -> list[dict[str, Any]]:
    """대기 중인 앱 검색을 최대 limit건 실행한다. 설정 오류면 아무것도 가져가지 않는다(행은 processing으로 남아 재시도)."""
    try:
        config = search_flow.load_search_config(settings)
    except search_flow.SearchConfigError as exc:
        logger.error("검색 작업을 실행할 수 없습니다: %s", exc)
        return []
    results: list[dict[str, Any]] = []
    for _ in range(limit):
        with db.transaction() as conn:
            claim = search_repository.claim_queued_search(
                conn, clock(), lease_seconds=settings.search_job_lease_seconds,
                max_attempts=settings.search_job_max_attempts)
        if claim is None:
            break
        search_id = claim["search_id"]
        if claim["action"] != "run":
            logger.warning("검색 %s 종료(%s)", search_id, claim["action"])
            results.append({"search_id": str(search_id), "action": claim["action"]})
            continue
        tago = _client(tago_client_factory or _default_tago, settings)
        daejeon = _client(daejeon_client_factory or _default_daejeon, settings)
        walking = _client(walking_client_factory or _default_walking, settings)
        try:
            outcome = search_flow.SearchWorkflow(
                db, config, tago_client=tago, walking_client=walking, clock=clock,
                realtime_provider=realtime_provider, collect=tago is not None or daejeon is not None,
                daejeon_client=daejeon,
            ).run_queued(search_id)
            results.append({"search_id": str(search_id), "action": "finished", "status": outcome.status,
                            "candidates": outcome.saved_candidates, "attempt": claim["attempts"]})
            logger.info("검색 %s 완료: %s (후보 %d건)", search_id, outcome.status, outcome.saved_candidates)
        except Exception as exc:  # 검색 행은 run_queued가 error로 남긴다.
            logger.exception("검색 %s 실행 실패", search_id)
            results.append({"search_id": str(search_id), "action": "failed", "error": type(exc).__name__})
        finally:
            for client in (tago, daejeon, walking):
                if client is not None:
                    client.close()
    return results


def _client(factory: Callable[[Settings], Any], settings: Settings) -> Any:
    try:
        return factory(settings)
    except (TagoConfigError, TmapConfigError, DaejeonBusConfigError) as exc:
        logger.warning("외부 클라이언트 미설정: %s", exc)
        return None


def _default_tago(settings: Settings) -> TagoClient:
    return TagoClient.from_settings(settings)


def _default_daejeon(settings: Settings) -> DaejeonBusClient:
    return DaejeonBusClient.from_settings(settings)


def _default_walking(settings: Settings) -> TmapWalkingClient:
    return TmapWalkingClient.from_settings(settings)
