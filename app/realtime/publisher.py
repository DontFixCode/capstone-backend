"""DB에 저장된 이동 상태를 연결된 앱에 전달한다.

워커는 결과·버전을 DB에 저장하고(app.journeys.guidance·result_version·state_version), API 서버는 자기 연결의 이동을
제한된 간격으로 확인해 버전이 바뀌면 최신 상태를 보낸다. 프로세스 간 메모리 공유를 가정하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.db.session import Database
from app.modules.journeys import repository as journey_repository
from app.modules.journeys import service as journey_service
from app.realtime.events import state_message


@dataclass
class Seen:
    state_version: int | None = None
    result_version: int | None = None


def load_state(db: Database, journey_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    """소유자의 이동 전체 상태 메시지. 없거나 다른 사용자 소유면 None."""
    with db.transaction() as conn:
        journey = journey_repository.get_journey(conn, journey_id, user_id)
    if journey is None:
        return None
    now = datetime.now(timezone.utc)
    view = journey_service.journey_view(journey, now)
    kind = "journey_state" if journey["lifecycle_status"] == "active" else "journey_ended"
    return state_message(view, sent_at=now, kind=kind)


def poll_versions(db: Database, journey_id: UUID) -> dict[str, Any] | None:
    with db.transaction() as conn:
        return journey_repository.stream_versions(conn, journey_id)


def mark_connection(db: Database, journey_id: UUID, state: str) -> None:
    with db.transaction() as conn:
        journey_repository.set_connection(conn, journey_id, state, datetime.now(timezone.utc))
