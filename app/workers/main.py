"""단일 수집 워커 실행 진입점.

    uv run python -m app.workers.main             # 상시 실행: 실시간 대상 주기 수집 + 정적 정보 하루 1회 갱신
    uv run python -m app.workers.main --once      # 만기 작업을 한 번 처리하고 종료
    uv run python -m app.workers.main --targets   # 설정 대상·최신 공개값·다음 예정 시각 출력(외부 호출 없음)
    uv run python -m app.workers.main --no-static # 정적 일일 갱신을 이 실행에서 제외
    uv run python -m app.workers.main --no-jobs   # 앱 검색 실행·이동 안내 재계산 스레드를 제외

정적 갱신 차례가 되면 버스 노선 형상도 이어서 수집한다(BACKEND_ROUTE_SHAPE_PROVIDER=odsay이고 일일 호출 한도
BACKEND_ODSAY_DAILY_CALL_LIMIT이 있을 때만, 남은 한도만큼. app/workflows/route_shapes.py).

상시 실행은 두 흐름을 같은 프로세스에서 돌린다: ① 공유 수집 루프(정적 갱신 → 실시간 → 표본 판정),
② 앱 작업 스레드(접수된 검색 실행 → 활성 이동 안내 재계산, BACKEND_JOB_POLL_SECONDS 간격). 두 흐름은 대상별 DB 잠금을
기다리지 않는 방식(try-lock)으로 같은 수집기를 써서 서로의 완료를 기다리며 막히지 않는다.

실시간 대상은 .env.api의 BACKEND_REALTIME_POSITION_ROUTE_IDS·BACKEND_REALTIME_ARRIVAL_STOP_IDS(쉼표 구분)이며,
매 회 설정을 다시 읽으므로 목록을 고치면 다음 회부터 등록·해제가 반영된다. 실제 외부 호출은 이 워커만 수행한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import Database, DbConfigError, DbError
from app.integrations.bus_api import TagoClient, TagoConfigError
from app.integrations import route_shapes_api
from app.integrations.daejeon_bus_api import DaejeonBusClient, DaejeonBusConfigError
from app.integrations.odsay_api import OdsayClient, OdsayConfigError
from app.modules.transit import repository as transit_repository
from app.modules.transit.service import RealtimeCollector, StaticImportService, latest_snapshot, utc_now
from app.workers import recalculation, scheduler, search_jobs
from app.workers.collection import collection_targets
from app.workflows import guidance as guidance_flow
from app.workflows import route_shapes as route_shapes_flow
from app.workflows.labeling import label_due

logger = logging.getLogger(__name__)


@dataclass
class PassResult:
    started_at: datetime
    targets: int = 0
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    static: dict[str, Any] | None = None
    shapes: dict[str, Any] | None = None
    labels: dict[str, int] | None = None
    next_wake_at: datetime | None = None


class Worker:
    def __init__(
        self,
        *,
        settings_factory: Callable[[], Settings] = Settings,
        db_factory: Callable[[Settings], Database] = Database.from_settings,
        client_factory: Callable[..., TagoClient] = TagoClient.from_settings,
        clock: Callable[[], datetime] = utc_now,
        static_enabled: bool = True,
        jobs_enabled: bool = True,
        walking_client_factory: Callable[[Settings], Any] | None = None,
        realtime_provider: Callable[..., Any] | None = None,
        daejeon_client_factory: Callable[[Settings], DaejeonBusClient] = DaejeonBusClient.from_settings,
        shape_client_factory: Callable[[Settings], OdsayClient] = OdsayClient.from_settings,
    ) -> None:
        self.settings_factory = settings_factory
        # 버스 노선 형상 제공처(ODsay). 제공처·일일 한도·키가 설정된 경우에만 정적 갱신 뒤에 호출한다.
        self.shape_client_factory = shape_client_factory
        # 실시간 도착정보 원천(대전시). 키가 없으면 도착정보 대상은 조회하지 않고 실패로 남긴다(위치정보 수집은 계속).
        self.daejeon_client_factory = daejeon_client_factory
        self.db_factory = db_factory
        self.client_factory = client_factory
        self.clock = clock
        self.static_enabled = static_enabled
        self.jobs_enabled = jobs_enabled
        # 검색·안내 작업용 주입점(격리 시험·실행기). 없으면 설정의 TMAP 앱키·공유 수집 최신값을 쓴다.
        self.walking_client_factory = walking_client_factory
        self.realtime_provider = realtime_provider
        self.network_cache = guidance_flow.NetworkCache()
        # 이 프로세스가 대상별로 마지막에 조회한 (위상 슬롯, 조회 종료 시각). 재시작하면 비며 그때는 간격 이상을 지킨다.
        self.ran_slots: dict[str, tuple[datetime, datetime]] = {}

    def run_jobs_pass(self) -> dict[str, Any]:
        """앱 작업 대기열 처리: 접수된 검색 실행 → 예약된 활성 이동 안내 재계산. 공유 수집 루프와 별도 스레드에서 돈다."""
        settings = self.settings_factory()
        db = self.db_factory(settings)
        searches = search_jobs.process_search_jobs(
            db, settings, clock=self.clock, tago_client_factory=lambda s: self.client_factory(s),
            walking_client_factory=self.walking_client_factory, realtime_provider=self.realtime_provider,
            daejeon_client_factory=self.daejeon_client_factory)
        journeys = recalculation.process_due_journeys(db, settings, clock=self.clock, network_cache=self.network_cache,
                                                      realtime_provider=self.realtime_provider)
        return {"searches": searches, "journeys": journeys}

    def run_jobs_forever(self, stop: threading.Event, sleep: Callable[[float], None] | None = None) -> None:
        wait = sleep or (lambda seconds: stop.wait(seconds))
        while not stop.is_set():
            try:
                result = self.run_jobs_pass()
                busy = bool(result["searches"])
            except (DbConfigError, TagoConfigError):
                raise
            except Exception:
                logger.exception("작업 처리 중 오류. 다음 확인 시각까지 대기합니다.")
                busy = False
            # 검색을 처리했으면 대기열에 남은 작업을 바로 확인하고, 아니면 확인 간격만큼 기다린다.
            if not busy:
                wait(self.settings_factory().job_poll_seconds)

    def run_pass(self) -> PassResult:
        settings = self.settings_factory()
        db = self.db_factory(settings)
        now = self.clock()
        result = PassResult(started_at=now)
        wake_candidates: list[datetime] = []
        # 정적 공개본이 실시간 관측의 노선·정류소·경유 위치 연결 기준이므로 만기면 먼저 갱신한다.
        if self.static_enabled:
            with db.transaction() as conn:
                is_due, due_at = scheduler.static_due(conn, settings.static_refresh_interval_seconds, now)
            if is_due:
                importer = StaticImportService(db, lambda **kw: self.client_factory(settings, **kw))
                report = importer.run()
                result.static = {"status": report.status, "data_version": report.data_version,
                                 "problems": [asdict(p) for p in report.problems]}
                logger.info("정적 갱신: %s", report.status)
                result.shapes = self.collect_route_shapes(settings, db)
                with db.transaction() as conn:
                    _, due_at = scheduler.static_due(conn, settings.static_refresh_interval_seconds, self.clock())
            wake_candidates.append(due_at)
            now = self.clock()
        interval = settings.realtime_interval_seconds
        with db.transaction() as conn:
            # 개발 설정 대상 + 유효기간 안의 검색이 등록한 대상(만료되면 자동 해제).
            targets = collection_targets(settings, conn, now)
            # 만료된 화면 조회 수요는 이미 집계에서 빠졌으므로 행만 정리한다(공개본·호출 이력은 그대로).
            transit_repository.delete_expired_view_targets(conn, now)
            plans = scheduler.plan_realtime(conn, targets, interval, now, self.ran_slots)
        result.targets = len(targets)
        # 해제된 대상의 슬롯 기억을 지운다. 다시 등록되면 DB 이력 기준으로 간격을 지킨다.
        active_keys = {p.target_key for p in plans}
        for key in list(self.ran_slots):
            if key not in active_keys:
                del self.ran_slots[key]
        due = [p for p in plans if p.due.at <= now]
        realtime_wake = [p.due.at for p in plans if p.due.at > now]
        if due:
            client = self.client_factory(settings)
            try:
                daejeon = self.daejeon_client_factory(settings)
            except DaejeonBusConfigError as exc:
                logger.warning("대전시 버스 API 미설정(도착정보 수집 안 함): %s", exc)
                daejeon = None
            collector = RealtimeCollector(db, client, interval_seconds=interval, clock=self.clock,
                                          daejeon_client=daejeon)
            try:
                for plan in due:
                    try:
                        # 실행 시점은 스케줄러가 간격 규칙으로 정했으므로 재사용 판정 없이 조회한다.
                        outcome = collector.collect(plan.target, force=True)
                    except (DbError, OSError) as exc:
                        logger.error("실시간 수집 실패 %s: %s", plan.target_key, exc)
                        result.outcomes.append({"target_key": plan.target_key, "action": "error", "reason": str(exc)})
                        continue
                    if outcome.http_attempts and plan.due.on_slot:
                        self.ran_slots[plan.target_key] = (plan.due.at, self.clock())
                    else:
                        self.ran_slots.pop(plan.target_key, None)
                    result.outcomes.append({**asdict(outcome), "due_reason": plan.due.reason})
                    logger.info("실시간 %s: %s (HTTP %d회, 관측 %d건)", plan.target_key, outcome.action,
                                outcome.http_attempts, outcome.observations)
            finally:
                client.close()
                if daejeon is not None:
                    daejeon.close()
            with db.transaction() as conn:
                replanned = scheduler.plan_realtime(conn, targets, interval, self.clock(), self.ran_slots)
            realtime_wake = [p.due.at for p in replanned]
        wake_candidates.extend(realtime_wake)
        # 환승 표본·공동 관측 묶음의 사후 판정(예측 값은 바꾸지 않고 관측·판정만 갱신).
        with db.transaction() as conn:
            result.labels = label_due(conn, self.clock())
        poll_limit = self.clock() + timedelta(seconds=settings.worker_poll_seconds)
        result.next_wake_at = min([poll_limit, *wake_candidates])
        return result

    def collect_route_shapes(self, settings: Settings, db: Database) -> dict[str, Any] | None:
        """버스 노선 형상 수집(하루 1회, 정적 갱신 직후). 실패해도 수집 루프는 계속한다. 제공처 미설정이면 None."""
        if route_shapes_api.configured_provider(settings.route_shape_provider) is None:
            return None
        if settings.odsay_daily_call_limit is None:
            logger.warning("형상 수집 건너뜀: BACKEND_ODSAY_DAILY_CALL_LIMIT이 비어 있습니다.")
            return {"status": "skipped", "reason": "daily_call_limit_unset"}
        try:
            with self.shape_client_factory(settings) as client:
                report = route_shapes_flow.collect(db, settings, client, clock=self.clock)
        except OdsayConfigError as exc:
            logger.warning("형상 수집 건너뜀: %s", exc)
            return {"status": "skipped", "reason": "api_key_unset"}
        except Exception:
            logger.exception("형상 수집 중 오류. 다음 정적 갱신 때 다시 시도합니다.")
            return {"status": "error"}
        logger.info("형상 수집: 호출 %d회, 저장 %d개 패턴, 남은 노선 %d개(ID 연결 %d), 중단 사유 %s", len(report.calls),
                    report.shapes_saved, report.pending_shapes, report.pending_mapping, report.stopped)
        return {"status": "done", **asdict(report)}

    def run_forever(self, stop: threading.Event, sleep: Callable[[float], None] | None = None) -> None:
        wait = sleep or (lambda seconds: stop.wait(seconds))
        while not stop.is_set():
            try:
                result = self.run_pass()
                delay = max(0.0, (result.next_wake_at - self.clock()).total_seconds())
            except (DbConfigError, TagoConfigError):
                raise
            except Exception:
                logger.exception("워커 실행 중 오류. 다음 확인 시각까지 대기합니다.")
                delay = self.settings_factory().worker_poll_seconds
            wait(delay)


def describe_targets(settings: Settings, db: Database,
                     ran_slots: dict[str, tuple[datetime, datetime]] | None = None) -> dict[str, Any]:
    """외부 호출 없이 설정 대상과 최신 공개값 상태, 다음 예정 시각을 보여준다."""
    now = utc_now()
    interval = settings.realtime_interval_seconds
    with db.transaction(isolation="REPEATABLE READ") as conn:
        targets = collection_targets(settings, conn, now)
        plans = scheduler.plan_realtime(conn, targets, interval, now, ran_slots)
        entries = []
        for plan in plans:
            snapshot = latest_snapshot(conn, plan.target, interval_seconds=interval, now=now)
            entries.append({
                "kind": plan.target.kind,
                "external_id": plan.target.external_id,
                "sources": sorted(plan.target.sources),
                "target_key": plan.target_key,
                "state": snapshot.state,
                "content": snapshot.content,
                "collected_at": snapshot.collected_at,
                "age_seconds": snapshot.age_seconds,
                "observations": len(snapshot.observations),
                "last_attempt": snapshot.last_attempt,
                "last_attempt_failed": snapshot.last_attempt_failed,
                "not_provided": snapshot.not_provided,
                "next_due_at": plan.due.at,
                "next_due_reason": plan.due.reason,
            })
    return {"checked_at": now, "interval_seconds": interval, "targets": entries}


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return float(value)
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.main", description="교통정보 단일 수집 워커")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="만기 작업을 한 번 처리하고 종료합니다.")
    mode.add_argument("--targets", action="store_true", help="설정 대상과 최신 상태를 출력합니다(외부 호출 없음).")
    parser.add_argument("--no-static", action="store_true", help="정적 정보 일일 갱신을 실행하지 않습니다.")
    parser.add_argument("--no-jobs", action="store_true",
                        help="앱 검색 실행·이동 안내 재계산 작업을 이 실행에서 제외합니다.")
    # 도움말에도 한글이 있으므로 인자 해석 전에 출력 인코딩 오류 처리를 바꾼다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    settings = Settings()
    configure_logging(settings.log_level)
    try:
        if args.targets:
            print(json.dumps(to_jsonable(describe_targets(settings, Database.from_settings(settings))),
                             ensure_ascii=False, indent=2))
            return 0
        worker = Worker(static_enabled=not args.no_static, jobs_enabled=not args.no_jobs)
        if args.once:
            result = {"collection": worker.run_pass()}
            if worker.jobs_enabled:
                result["jobs"] = worker.run_jobs_pass()
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
            return 0
        stop = threading.Event()
        logger.info("수집 워커 시작 (실시간 간격 %ss, 정적 갱신 %s, 앱 작업 %s)", settings.realtime_interval_seconds,
                    "포함" if not args.no_static else "제외", "포함" if worker.jobs_enabled else "제외")
        jobs_thread = None
        if worker.jobs_enabled:
            jobs_thread = threading.Thread(target=worker.run_jobs_forever, args=(stop,), name="app-jobs", daemon=True)
            jobs_thread.start()
        try:
            worker.run_forever(stop)
        except KeyboardInterrupt:
            stop.set()
        finally:
            stop.set()
            if jobs_thread is not None:
                jobs_thread.join(timeout=30)
        return 0
    except (DbConfigError, DbError, TagoConfigError) as exc:
        print(f"설정·연결 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
