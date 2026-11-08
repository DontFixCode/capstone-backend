"""Phase 2 교통정보 저장·공개·실시간 수집의 PostgreSQL+PostGIS 격리 시험.

대상 DB가 아니라 TEST_POSTGIS_DSN(예: postgresql://postgres:<pw>@127.0.0.1:55432/postgres)이 가리키는 임시 서버에서
시험마다 새 데이터베이스를 만들고, 대상 DB에 적용된 DDL 사본(migrations/01_transit_core_supabase.sql)을 그대로 적용한다.
TAGO 응답은 tests/fixtures/tago_world.py의 합성 데이터다. 환경변수가 없으면 건너뛴다.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55432/postgres"
    uv run python -m unittest discover -s tests/integration
"""

import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from tago_world import FAKE_KEY, FakeTago, Fault  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.session import Database, DbParams, advisory_key  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.modules.transit import repository, service  # noqa: E402
from app.modules.transit.schemas import RealtimeTarget  # noqa: E402
from app.workers.main import Worker  # noqa: E402

DSN = os.environ.get("TEST_POSTGIS_DSN")
DDL = ROOT / "migrations" / "01_transit_core_supabase.sql"
UTC = timezone.utc
R1, R2, R3 = "DJB9000001", "DJB9000002", "DJB9000003"


def make_client(fake, **options):
    options.setdefault("max_retries", 1)
    return TagoClient(FAKE_KEY, transport=fake.transport(), request_interval=0, retry_backoff=0,
                      sleep=lambda _s: None, **options)


@unittest.skipUnless(DSN, "TEST_POSTGIS_DSN이 없어 DB 격리 시험을 건너뜀")
class DbTestCase(unittest.TestCase):
    def setUp(self):
        url = urlparse(DSN)
        self.dbname = f"phase2_{uuid4().hex[:12]}"
        self.admin = psycopg.connect(DSN, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        self.addCleanup(self._drop)
        params = DbParams(url.hostname, url.port or 5432, self.dbname, url.username, url.password or "", 10)
        self.db = Database(params)
        with psycopg.connect(DSN.rsplit("/", 1)[0] + f"/{self.dbname}", autocommit=True) as conn:
            conn.execute("CREATE SCHEMA extensions")
            conn.execute("CREATE EXTENSION postgis SCHEMA extensions")
            conn.execute("CREATE SCHEMA auth")
            conn.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
            # 대상 DB와 같은 12개 테이블 구조(적용 DDL 사본 01~08 + 파일에 없는 journey_id FK).
            for path in sorted(DDL.parent.glob("[0-9]*.sql")):
                conn.execute("\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                                       if not line.startswith("\\")))
            conn.execute("ALTER TABLE app.route_searches ADD CONSTRAINT route_searches_journey_id_fkey "
                         "FOREIGN KEY (journey_id) REFERENCES app.journeys(id) ON DELETE CASCADE")
        self.fake = FakeTago()

    def _drop(self):
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')
        self.admin.close()

    def importer(self, fake=None, **options):
        fake = fake or self.fake
        return service.StaticImportService(self.db, lambda **kw: make_client(fake, **options, **kw), rows=2)

    def query(self, sql, params=None):
        with self.db.transaction() as conn:
            return conn.execute(sql, params).fetchall()


class StaticImportTest(DbTestCase):
    def test_initial_load_publishes_one_version_and_matches_source(self):
        report = self.importer().run()
        self.assertEqual(report.status, "published", report.problems)
        self.assertEqual(report.verification["mismatches"], [])
        version = report.data_version
        for table in ("stops", "routes", "route_stops"):
            self.assertEqual(self.query(f"SELECT DISTINCT data_version FROM app.{table}"), [(version,)], table)
        counts = dict(self.query("SELECT 'stops', count(*) FROM app.stops UNION ALL "
                                 "SELECT 'routes', count(*) FROM app.routes UNION ALL "
                                 "SELECT 'route_stops', count(*) FROM app.route_stops"))
        self.assertEqual(counts, {"stops": 7, "routes": 3, "route_stops": 11})
        # 반복 경유는 서로 다른 seq로 모두 보존되고, 시외 경유 정류소도 정류소로 보존된다.
        r1 = self.query("SELECT rs.seq, s.external_id, rs.direction_code, rs.source_node_order FROM app.route_stops rs "
                        "JOIN app.routes r ON r.id = rs.route_id JOIN app.stops s ON s.id = rs.stop_id "
                        "WHERE r.external_id = %s ORDER BY rs.seq", (R1,))
        self.assertEqual([row[1] for row in r1], ["DJB0000001", "DJB0000002", "DJB0000003", "DJB0000002", "DJB0000001"])
        self.assertEqual([row[2] for row in r1], ["0", "0", "1", "1", "1"])
        self.assertEqual(self.query("SELECT location IS NULL FROM app.stops WHERE external_id IN "
                                    "('DJB0000005','DJB0000006','CCB0000009') ORDER BY external_id"),
                         [(False,), (True,), (True,)])
        self.assertEqual(self.query("SELECT count(*) FROM app.api_fetches WHERE published_at IS NULL"), [(0,)])
        self.assertEqual(self.query("SELECT count(DISTINCT published_at) FROM app.api_fetches"), [(1,)])
        service_info = self.query("SELECT service_info FROM app.routes WHERE external_id = %s", (R3,))[0][0]
        self.assertEqual(service_info["fields"]["intervaltime"]["state"], "missing", "미제공 운행정보를 채우지 않음")
        with self.db.transaction() as conn:
            status = service.static_status(conn)
        self.assertEqual((status["state"], status["mixed_versions"]), ("published", False))

    def test_verify_rereads_raw_bodies_and_detects_tampering(self):
        self.importer().run()
        with self.db.transaction() as conn:
            ok = service.verify_published_static(conn)
        self.assertEqual((ok["state"], ok["mismatches"]), ("verified", []))
        self.assertEqual(ok["source"]["route_only_stops"], 1)
        self.assertEqual(ok["loaded"], {"active_stops": 7, "active_routes": 3, "route_stop_entries": 11})
        with self.db.transaction() as conn:
            conn.execute("UPDATE app.route_stops SET direction_code = '9' WHERE seq = 1")
            conn.execute("UPDATE app.stops SET is_active = FALSE WHERE external_id = 'CCB0000009'")
            bad = service.verify_published_static(conn)
            conn.rollback()
        self.assertEqual(bad["state"], "mismatch")
        self.assertEqual(len(bad["mismatches"]), 2, bad["mismatches"])

    def test_failed_refresh_keeps_last_good_and_records_calls(self):
        first = self.importer().run()
        before = self.query("SELECT external_id, id, name, data_version FROM app.stops ORDER BY 1")
        self.fake.world["stations"][0]["nodenm"] = "바뀐이름"
        self.fake.faults.append(Fault("getRouteAcctoThrghSttnList", "http500", match={"routeId": R3}))
        attempts_before = self.query("SELECT count(*) FROM app.api_fetches")[0][0]
        report = self.importer().run()
        self.assertEqual(report.status, "held")
        self.assertEqual(self.query("SELECT external_id, id, name, data_version FROM app.stops ORDER BY 1"), before)
        self.assertEqual(self.query("SELECT DISTINCT data_version FROM app.route_stops"), [(first.data_version,)])
        failed = self.query("SELECT result_status, attempt_no, error_reason FROM app.api_fetches "
                            "WHERE target_key LIKE %s AND published_at IS NULL ORDER BY id",
                            (f"%getRouteAcctoThrghSttnList%routeId={R3}",))
        self.assertEqual([(s, n) for s, n, _ in failed], [("error", 1), ("error", 2)], "실패·재시도 모두 기록")
        self.assertTrue(all("static_held" in (r or "") for _, _, r in failed))
        attempts_after = self.query("SELECT count(*) FROM app.api_fetches")[0][0]
        self.assertEqual(attempts_after - attempts_before, self.fake.count() - report_calls_first(first))
        with self.db.transaction() as conn:
            status = service.static_status(conn)
        self.assertEqual(status["state"], "held_or_running")
        self.assertTrue(status["reasons_since_last_publish"])

    def test_missing_page_is_held_without_touching_published_rows(self):
        self.importer().run()
        version = self.query("SELECT DISTINCT data_version FROM app.routes")
        self.fake.faults.append(Fault("getSttnNoList", "empty_page", page=3))
        report = self.importer().run()
        self.assertEqual(report.status, "held")
        self.assertEqual(self.query("SELECT DISTINCT data_version FROM app.routes"), version)
        partial = self.query("SELECT page_no, result_status FROM app.api_fetches WHERE api_kind = 'stops' "
                             "AND published_at IS NULL ORDER BY page_no")
        self.assertEqual(partial, [(1, "partial"), (2, "partial"), (3, "partial")], "빈 페이지도 정상 수신이나 배치 불완전")

    def test_repeat_run_keeps_internal_ids_and_deactivates_only_after_full_validation(self):
        first = self.importer().run()
        ids = dict(self.query("SELECT external_id, id FROM app.stops"))
        route_ids = dict(self.query("SELECT external_id, id FROM app.routes"))
        # 관측이 참조하는 정류소는 사라져도 물리 삭제하지 않고 비활성화한다.
        with self.db.transaction() as conn:
            fetch_id = conn.execute("SELECT id FROM app.api_fetches LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO app.transit_observations (fetch_id, item_index, kind, stop_id, collected_at) "
                         "VALUES (%s, 9999, 'arrival', %s, now())", (fetch_id, ids["DJB0000006"]))
        world = self.fake.world
        world["stations"] = [s for s in world["stations"] if s["nodeid"] != "DJB0000006"]
        world["routes"][R3]["stops"] = world["routes"][R3]["stops"][:2]
        del world["routes"][R2]
        second = self.importer().run()
        self.assertEqual(second.status, "published", second.problems)
        self.assertNotEqual(first.data_version, second.data_version)
        after = dict(self.query("SELECT external_id, id FROM app.stops"))
        self.assertEqual(after, ids, "내부 ID 유지, 행 삭제 없음")
        self.assertEqual(dict(self.query("SELECT external_id, id FROM app.routes")), route_ids)
        inactive = self.query("SELECT external_id FROM app.stops WHERE NOT is_active ORDER BY 1")
        self.assertEqual(inactive, [("CCB0000009",), ("DJB0000006",)])
        self.assertEqual(self.query("SELECT external_id FROM app.routes WHERE NOT is_active"), [(R2,)])
        self.assertEqual(self.query("SELECT count(*) FROM app.route_stops"), [(7,)])
        self.assertEqual(self.query("SELECT DISTINCT data_version FROM app.route_stops"), [(second.data_version,)])
        third = self.importer().run()
        self.assertEqual((third.status, third.verification["mismatches"]), ("published", []))
        self.assertEqual(self.query("SELECT count(*) FROM app.stops"), [(7,)], "반복 실행에도 중복 행 없음")

    def test_concurrent_run_is_skipped_without_http_calls(self):
        holder = self.db.connect()
        try:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key(service.STATIC_LOCK),))
            report = self.importer().run()
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(report.status, "skipped_locked")
        self.assertEqual(self.fake.count(), 0)
        self.assertEqual(self.query("SELECT count(*) FROM app.api_fetches"), [(0,)])

    def test_reader_never_sees_mixed_versions_during_publish(self):
        first = self.importer().run()
        reader = self.db.connect()
        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        before = reader.execute("SELECT count(*), array_agg(DISTINCT data_version) FROM app.route_stops").fetchone()
        self.fake.world["routes"][R1]["stops"].pop()
        second = self.importer().run()
        during = reader.execute("SELECT count(*), array_agg(DISTINCT data_version) FROM app.route_stops").fetchone()
        stops_version = reader.execute("SELECT array_agg(DISTINCT data_version) FROM app.stops").fetchone()[0]
        reader.rollback()
        reader.close()
        self.assertEqual(before, during, "진행 중인 읽기 트랜잭션은 교체 전 공개본만 본다")
        self.assertEqual(stops_version, [first.data_version])
        network = service.read_static_network(self.db)
        self.assertEqual(network.data_version, second.data_version)
        self.assertEqual(sum(len(p.stops) for p in network.patterns), 10)

    def test_static_network_fits_phase3_input_contract(self):
        """Phase 3의 기존 입력 형식(routing.schemas.StaticSnapshot)으로 옮겨 기존 인덱스 함수가 받는지만 확인한다."""
        from app.modules.routing import schemas as routing_schemas
        from app.modules.routing.service import build_search_index

        self.importer().run()
        network = service.read_static_network(self.db)
        snapshot = routing_schemas.StaticSnapshot(
            snapshot_id=str(network.data_version),
            stops=tuple(routing_schemas.StopRecord(stop_id=s.external_id, name=s.name, lat=s.lat, lon=s.lon)
                        for s in network.stops),
            patterns=tuple(routing_schemas.RoutePattern(
                pattern_id=p.pattern_key, route_id=p.route_external_id, direction=None,
                stops=tuple(routing_schemas.PatternStop(stop_id=s.stop_external_id, stop_order=s.seq)
                            for s in p.stops)) for p in network.patterns),
        )
        index = build_search_index(snapshot, transfer_radius_meters=0)
        self.assertEqual(len(index.pattern_stops), 3)
        self.assertEqual(len(index.boardings_by_stop["DJB0000002"]), 2, "반복 경유 위치를 각각 탑승 위치로 인식")

    def test_publish_failure_rolls_back_but_keeps_http_history(self):
        original = repository.publish_static

        def broken(conn, *args, **kwargs):
            original(conn, *args, **kwargs)
            raise RuntimeError("synthetic publish failure")

        with mock.patch.object(repository, "publish_static", side_effect=broken):
            report = self.importer().run()
        self.assertEqual(report.status, "held")
        self.assertEqual(self.query("SELECT count(*) FROM app.stops"), [(0,)])
        rows = self.query("SELECT count(*), count(*) FILTER (WHERE published_at IS NULL), "
                          "count(*) FILTER (WHERE error_reason LIKE 'static_publish_failed%%') FROM app.api_fetches")[0]
        self.assertEqual(rows[0], self.fake.count())
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[2], rows[0])

    def test_nearby_uses_meter_radius_and_ignores_unknown_coordinates(self):
        self.importer().run()
        near = service.find_nearby_stops(self.db, 36.3500, 127.3800, 200)
        self.assertEqual([s.external_id for s in near], ["DJB0000001", "DJB0000002"])
        self.assertLess(near[0].distance_m, 1)
        self.assertTrue(140 < near[1].distance_m < 150, near[1].distance_m)
        everywhere = service.find_nearby_stops(self.db, 36.4, 127.4, 50_000_000, limit=100)
        self.assertNotIn("DJB0000006", [s.external_id for s in everywhere], "좌표 미확인 정류소 제외")
        with self.assertRaises(ValueError):
            service.find_nearby_stops(self.db, 36.35, 127.38, 0)


def report_calls_first(report):
    return report.calls["http_attempts"]


class RealtimeTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.importer().run().status, "published")
        self.now = datetime.now(UTC)
        self.client = make_client(self.fake)

    def collector(self, clock=None):
        return service.RealtimeCollector(self.db, self.client, interval_seconds=600, rows=2,
                                         clock=clock or (lambda: self.now))

    def latest(self, target, now=None):
        with self.db.transaction() as conn:
            return service.latest_snapshot(conn, target, interval_seconds=600, now=now or datetime.now(UTC))

    def test_first_fetch_reuse_refresh(self):
        target = RealtimeTarget("positions", R1)
        before = self.fake.count("getRouteAcctoBusLcList")
        first = self.collector().collect(target)
        self.assertEqual((first.action, first.observations, first.http_attempts), ("published", 3, 2))
        reused = self.collector().collect(target)
        self.assertEqual(reused.action, "reused")
        self.assertEqual(self.fake.count("getRouteAcctoBusLcList") - before, 2, "간격 이내에는 외부 호출 없음")
        later = self.collector(clock=lambda: datetime.now(UTC) + timedelta(seconds=601)).collect(target)
        self.assertEqual(later.action, "published")
        snap = self.latest(target)
        self.assertEqual((snap.state, snap.content, snap.pages, len(snap.observations)), ("fresh", "items", 2, 3))
        self.assertEqual(snap.batch_key, later.batch_key)
        stale = self.latest(target, now=datetime.now(UTC) + timedelta(seconds=700))
        self.assertEqual(stale.state, "stale")

    def test_position_fields_keep_source_and_server_time_apart(self):
        self.collector().collect(RealtimeTarget("positions", R1))
        obs = self.latest(RealtimeTarget("positions", R1)).observations
        first, mismatch, zero = obs
        self.assertEqual((first["vehicle_no"], first["stop_seq"], first["route_external_id"]), ("대전70자1001", 2, R1))
        self.assertEqual(first["details"]["position_match"]["status"], "matched")
        self.assertIsNone(first["source_generated_at"], "원천 관측 시각 미제공")
        self.assertIsNone(first["run_key"])
        self.assertIsNone(mismatch["stop_seq"])
        self.assertEqual(mismatch["details"]["position_match"]["reason"], "nodeord_stop_mismatch")
        self.assertEqual((zero["lat"], zero["lon"], zero["details"]["location_status"]), (None, None, "zero"))
        received = self.query("SELECT received_at FROM app.api_fetches WHERE id = %s", (first["fetch_id"],))[0][0]
        self.assertEqual(first["collected_at"], received, "collected_at은 서버 수신 시각")

    def test_arrivals_do_not_link_vehicle_or_invent_eta(self):
        self.collector().collect(RealtimeTarget("arrivals", "DJB0000003"))
        obs = self.latest(RealtimeTarget("arrivals", "DJB0000003")).observations
        self.assertEqual([o["remaining_seconds"] for o in obs], [95, 0])
        for o in obs:
            self.assertIsNone(o["vehicle_no"])
            self.assertIsNone(o["eta_basis_at"])
            self.assertIsNone(o["predicted_arrival_at"])
            self.assertEqual(o["details"]["vehicle_match"]["status"], "not_provided")
        self.assertEqual([o["stop_seq"] for o in obs], [3, 1], "현재 패턴에서 한 번만 경유하면 경유 위치 확정")
        self.collector().collect(RealtimeTarget("arrivals", "DJB0000002"))
        repeated = self.latest(RealtimeTarget("arrivals", "DJB0000002")).observations[0]
        self.assertIsNone(repeated["stop_seq"])
        self.assertEqual(repeated["details"]["visit_match"]["candidate_seqs"], [2, 4])

    def test_empty_is_published_failure_keeps_previous(self):
        target = RealtimeTarget("arrivals", "DJB0000004")
        empty = self.collector().collect(target)
        self.assertEqual(empty.action, "published_empty")
        snap = self.latest(target)
        self.assertEqual((snap.state, snap.content, snap.last_attempt_failed), ("fresh", "empty", False))
        self.fake.faults.append(Fault("getSttnAcctoArvlPrearngeInfoList", "http500"))
        failed = self.collector().collect(target, force=True)
        self.assertEqual((failed.action, failed.http_attempts), ("failed", 2))
        after = self.latest(target)
        self.assertEqual((after.content, after.batch_key, after.last_attempt_failed), ("empty", empty.batch_key, True))
        self.assertEqual(after.last_attempt.result_status, "error")
        never = self.latest(RealtimeTarget("arrivals", "DJB0000001"))
        self.assertEqual((never.state, never.content), ("no_data", None))
        self.assertIn("vehicle_no", never.not_provided)

    def test_partial_batch_is_not_published(self):
        target = RealtimeTarget("positions", R1)
        published = self.collector().collect(target)
        self.fake.faults.append(Fault("getRouteAcctoBusLcList", "code", page=2, code="99"))
        partial = self.collector().collect(target, force=True)
        self.assertEqual(partial.action, "partial")
        snap = self.latest(target)
        self.assertEqual(snap.batch_key, published.batch_key)
        self.assertTrue(snap.last_attempt_failed)
        rows = self.query("SELECT page_no, result_status FROM app.api_fetches WHERE batch_key = %s ORDER BY page_no",
                          (partial.batch_key,))
        self.assertEqual(rows, [(1, "partial"), (2, "error")])
        self.assertEqual(self.query("SELECT count(*) FROM app.transit_observations WHERE fetch_id IN "
                                    "(SELECT id FROM app.api_fetches WHERE batch_key = %s)", (partial.batch_key,)),
                         [(0,)])

    def test_lock_prevents_duplicate_calls(self):
        target = RealtimeTarget("positions", R1)
        key = service.realtime_target_key(target)
        holder = self.db.connect()
        try:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key(f"transit:realtime:{key}"),))
            before = self.fake.count()
            outcome = self.collector().collect(target, force=True)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual((outcome.action, self.fake.count() - before), ("in_progress", 0))

    def test_parallel_collects_call_once(self):
        target = RealtimeTarget("positions", R1)
        before = self.fake.count()
        results = []

        def run():
            client = make_client(self.fake)
            results.append(service.RealtimeCollector(self.db, client, interval_seconds=600, rows=2).collect(target))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        actions = sorted(r.action for r in results)
        self.assertEqual(actions.count("published"), 1, actions)
        self.assertEqual(self.fake.count() - before, 2, "4개 동시 요청에도 실제 호출은 한 배치")

    def test_usage_counts_pages_retries_and_failures(self):
        self.fake.faults.append(Fault("getRouteAcctoBusLcList", "http500", page=2, times=1))
        self.collector().collect(RealtimeTarget("positions", R1))
        report = service.usage_report(self.db, Settings(_env_file=None), datetime.now(UTC) - timedelta(hours=1),
                                      datetime.now(UTC) + timedelta(minutes=1))
        loc = report["services"]["BusLcInfoInqireService"]
        self.assertEqual(loc["by_api_kind"]["positions"]["http_attempts"], 3)
        self.assertEqual(loc["by_api_kind"]["positions"]["retries"], 1)
        self.assertEqual(loc["by_api_kind"]["positions"]["later_pages"], 2)
        self.assertEqual(loc["by_api_kind"]["positions"]["failed"], 1)
        self.assertEqual(loc["approved_limit"], "미확인")
        static = report["services"]["BusRouteInfoInqireService"]["http_attempts"]
        self.assertEqual(static, self.fake.count("getRouteNoList") + self.fake.count("getRouteInfoIem")
                         + self.fake.count("getRouteAcctoThrghSttnList"))

    def test_no_secret_is_stored(self):
        self.fake.faults.append(Fault("getSttnAcctoArvlPrearngeInfoList", "echo_key"))
        self.collector().collect(RealtimeTarget("arrivals", "DJB0000003"))
        dump = self.query("SELECT string_agg(coalesce(raw_body,'') || request_params::text || coalesce(error_reason,''),"
                          " '') FROM app.api_fetches")[0][0]
        for form in (FAKE_KEY, "Synthetic%2BKey", "Synthetic+Key"):
            self.assertNotIn(form, dump)
        self.assertEqual(self.query("SELECT count(*) FROM app.api_fetches WHERE request_params ? 'serviceKey'"), [(0,)])


class WorkerTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.ids = {"positions": [R1], "arrivals": []}
        self.now = datetime.now(UTC)

    def settings(self):
        return Settings(_env_file=None, realtime_position_route_ids=",".join(self.ids["positions"]),
                        realtime_arrival_stop_ids=",".join(self.ids["arrivals"]))

    def worker(self, static=True):
        return Worker(settings_factory=self.settings, db_factory=lambda _s: self.db,
                      client_factory=lambda _s, **kw: make_client(self.fake, **kw), clock=lambda: self.now,
                      static_enabled=static)

    def test_register_share_refresh_and_release(self):
        worker = self.worker()
        first = worker.run_pass()
        self.assertEqual(first.static["status"], "published", "정적 공개본이 없으면 즉시 초기 적재")
        self.assertEqual([o["action"] for o in first.outcomes], ["published"])
        self.assertEqual(first.outcomes[0]["due_reason"], "initial")
        calls = self.fake.count("getRouteAcctoBusLcList")
        self.now += timedelta(seconds=30)
        self.ids["arrivals"] = ["DJB0000003"]
        second = worker.run_pass()
        self.assertEqual([o["target_key"].split("?")[0] for o in second.outcomes],
                         ["tago:getSttnAcctoArvlPrearngeInfoList"], "새로 등록한 대상만 최초 조회")
        self.assertEqual(self.fake.count("getRouteAcctoBusLcList"), calls, "간격 전 위치 재호출 없음")
        self.assertIsNone(second.static, "정적 갱신은 하루 1회")
        self.now += timedelta(seconds=1250)
        third = worker.run_pass()
        self.assertEqual(sorted(o["action"] for o in third.outcomes), ["published", "published"])
        self.ids["positions"] = []
        self.now += timedelta(seconds=1250)
        fourth = worker.run_pass()
        self.assertEqual([o["target_key"].split("?")[0] for o in fourth.outcomes],
                         ["tago:getSttnAcctoArvlPrearngeInfoList"], "해제한 대상은 더 수집하지 않음")
        self.assertNotIn(service.realtime_target_key(RealtimeTarget("positions", R1)), worker.ran_slots)
        self.now += timedelta(days=1, seconds=1)
        self.assertEqual(worker.run_pass().static["status"], "published", "하루 뒤 정적 갱신")


if __name__ == "__main__":
    unittest.main()
