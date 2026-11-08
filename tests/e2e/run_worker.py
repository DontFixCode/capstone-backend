"""E2E용 워커 프로세스: 실제 Worker(공유 수집 루프 + 앱 작업 스레드)를 별도 프로세스로 실행한다.

TAGO는 합성 서버(tests/fixtures), 실시간 ETA는 기준 시각이 확인된 합성 입력(실제 TAGO 기본 도착정보에는 기준 시각이 없음)이다.
E2E_REAL_TMAP=1이면 .env.api의 SK_OPEN_API_KEY로 실제 TMAP 보행자 경로를 호출하고(합성 정류소 좌표는 대전 시내), 아니면 합성 TMAP.
"""

import os
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import phase3_world as w  # noqa: E402
from tago_world import FAKE_KEY, FakeTago  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.session import Database, DbParams  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient  # noqa: E402
from app.modules.evaluation.schemas import EtaReport  # noqa: E402
from app.workers.main import Worker  # noqa: E402
from app.workflows import search as flow  # noqa: E402
from e2e_settings import e2e_settings  # noqa: E402

configure_logging("INFO")
url = urlparse(os.environ["E2E_DSN"])
db = Database(DbParams(url.hostname, url.port or 5432, url.path.lstrip("/"), url.username, url.password or "", 10))
tago = FakeTago(w.world())
fake_tmap = w.FakeTmap()
real_tmap = os.environ.get("E2E_REAL_TMAP") == "1"


def tago_client(_settings, **kw):
    kw.setdefault("max_retries", 0)
    return TagoClient(FAKE_KEY, transport=tago.transport(), request_interval=0, retry_backoff=0,
                      sleep=lambda _s: None, **kw)


def walking_client(_settings):
    if real_tmap:
        return TmapWalkingClient.from_settings(Settings())  # 실제 앱키(.env.api), 값은 출력하지 않음
    return TmapWalkingClient(fake_tmap.app_key, transport=fake_tmap.transport())


def provider(conn, targets, network, now):
    rows = w.synthetic_eta_rows(network.patterns, basis_at=now - timedelta(seconds=30))
    return flow.RealtimeInputs([EtaReport(**row) for row in rows], frozenset(w.STOPS), {}, [("synthetic", "e2e")],
                               {})


settings = e2e_settings()
worker = Worker(settings_factory=lambda: settings, db_factory=lambda _s: db, client_factory=tago_client,
                walking_client_factory=walking_client, realtime_provider=provider, static_enabled=False)
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
jobs = threading.Thread(target=worker.run_jobs_forever, args=(stop,), name="app-jobs", daemon=True)
jobs.start()
print("worker ready", flush=True)
try:
    worker.run_forever(stop)
except KeyboardInterrupt:
    pass
finally:
    stop.set()
    jobs.join(timeout=10)
