"""이 API 프로세스의 WebSocket 연결 수 관리.

연결 목록은 프로세스 메모리에만 있다(API 서버와 워커가 공유하지 않음). 연결 수는 journeys.connection_state를 기록할 때만 쓰며,
이동 생명주기·재평가 예약·수집 대상과는 무관하다. 마지막 연결이 끊겨도 이동은 그대로 active다.
"""

from __future__ import annotations

import threading
from collections import Counter
from uuid import UUID


class ConnectionRegistry:
    def __init__(self) -> None:
        self._counts: Counter[UUID] = Counter()
        self._lock = threading.Lock()

    def add(self, journey_id: UUID) -> int:
        with self._lock:
            self._counts[journey_id] += 1
            return self._counts[journey_id]

    def remove(self, journey_id: UUID) -> int:
        with self._lock:
            self._counts[journey_id] -= 1
            remaining = self._counts[journey_id]
            if remaining <= 0:
                del self._counts[journey_id]
                return 0
            return remaining

    def count(self, journey_id: UUID) -> int:
        with self._lock:
            return self._counts.get(journey_id, 0)


registry = ConnectionRegistry()
