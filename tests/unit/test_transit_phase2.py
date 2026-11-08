"""Phase 2 교통정보 수집 규칙의 DB 없는 시험. 응답은 tests/fixtures/tago_world.py의 합성 데이터다."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

from tago_world import FAKE_KEY, FakeTago, Fault  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.integrations.bus_api import TagoClient  # noqa: E402
from app.modules.transit import service  # noqa: E402
from app.modules.transit.schemas import RealtimeTarget  # noqa: E402
from app.workers import scheduler  # noqa: E402
from app.workers.collection import configured_targets  # noqa: E402

UTC = timezone.utc


def client_for(fake: FakeTago, **options) -> TagoClient:
    return TagoClient(FAKE_KEY, transport=fake.transport(), request_interval=0, retry_backoff=0,
                      sleep=lambda _s: None, **options)


class MemoryRecorder(service.FetchRecorder):
    """DB 대신 메모리에 시도 행을 쌓는다. 실제 기록 형식(FetchAttemptRow)은 같다."""

    def __init__(self):
        self.conn = None
        self.batch = None
        self.rows = []
        from collections import Counter
        self.counts = Counter()

    def __call__(self, record):
        with mock.patch.object(service.repository, "insert_fetch", side_effect=self._insert):
            super().__call__(record)

    def _insert(self, _conn, row):
        self.rows.append(row)
        return len(self.rows)


def collect_static(fake: FakeTago, rows: int = 2):
    recorder = MemoryRecorder()
    client = client_for(fake, on_attempt=recorder, max_retries=1)
    importer = service.StaticImportService(db=None, client_factory=None, rows=rows)
    batches = importer._collect(client, recorder)
    return batches, recorder


class SettingsTest(unittest.TestCase):
    def test_target_lists_are_comma_separated_strings_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env.api"
            env.write_text("BACKEND_REALTIME_POSITION_ROUTE_IDS= DJB1 ,DJB2,DJB1,\n"
                           "BACKEND_REALTIME_ARRIVAL_STOP_IDS=0012\nBACKEND_TAGO_LIMIT_ARRIVALS=\n", encoding="utf-8")
            settings = Settings(_env_file=env)
        self.assertEqual(settings.realtime_position_route_ids, ("DJB1", "DJB2"))
        self.assertEqual(settings.realtime_arrival_stop_ids, ("0012",), "앞자리 0을 보존하는 문자열 ID")
        self.assertIsNone(settings.tago_limit_arrivals, "빈 한도는 미확인(None)")
        self.assertEqual((settings.realtime_interval_seconds, settings.static_refresh_interval_seconds), (600, 86400))
        targets = configured_targets(settings)
        self.assertEqual([(t.kind, t.external_id) for t in targets],
                         [("arrivals", "0012"), ("positions", "DJB1"), ("positions", "DJB2")])

    def test_merge_targets_combines_sources_for_same_query(self):
        merged = service.merge_targets([
            [RealtimeTarget("positions", "R1", frozenset({"dev_config"}))],
            [RealtimeTarget("positions", "R1", frozenset({"search"})), RealtimeTarget("arrivals", "R1")],
        ])
        self.assertEqual(len(merged), 2, "kind가 다르면 다른 대상")
        self.assertEqual(merged[1].sources, frozenset({"dev_config", "search"}))


class AttemptRecordTest(unittest.TestCase):
    def test_every_http_attempt_is_recorded_with_redacted_body(self):
        fake = FakeTago()
        fake.faults.append(Fault("getSttnNoList", "http500", page=2, times=1))
        fake.faults.append(Fault("getSttnNoList", "echo_key", page=3))
        recorder = MemoryRecorder()
        client = client_for(fake, on_attempt=recorder, max_retries=1)
        recorder.start("stops", "k", service.STATIC_NORMALIZER_VERSION)
        collection = client.station_list(rows=2)
        recorder.finish(collection)
        self.assertEqual(collection.status, "success")
        self.assertEqual([(r.page_no, r.attempt_no, r.result_status) for r in recorder.rows],
                         [(1, 1, "success"), (2, 1, "error"), (2, 2, "success"), (3, 1, "success")])
        self.assertEqual(recorder.counts["retries"], 1)
        for row in recorder.rows:
            self.assertNotIn("serviceKey", row.request_params)
            for text in (row.raw_body or "", row.error_reason or "", str(row.request_params)):
                self.assertNotIn(FAKE_KEY, text)
                self.assertNotIn("Synthetic%2BKey", text)
        self.assertIn("serviceKey=***", recorder.rows[-1].raw_body, "반사된 URL의 인증키를 가림")
        self.assertEqual(recorder.rows[1].http_status, 500)
        self.assertIsNotNone(recorder.rows[1].received_at)

    def test_timeout_attempt_has_no_received_at_or_body(self):
        fake = FakeTago()
        fake.faults.append(Fault("getRouteAcctoBusLcList", "timeout"))
        recorder = MemoryRecorder()
        client = client_for(fake, on_attempt=recorder, max_retries=0)
        recorder.start("positions", "k", service.POSITIONS_NORMALIZER_VERSION)
        client.bus_locations("DJB9000001", rows=2)
        row = recorder.rows[0]
        self.assertEqual((row.result_status, row.received_at, row.raw_body, row.http_status), ("timeout", None, None, None))

    SESSION_MSG = "가용한 세션이 존재하지 않습니다. (30/30)"

    def test_session_exhaustion_waits_and_retries_only_when_enabled(self):
        fake = FakeTago()
        fake.faults.append(Fault("getSttnNoList", "code", page=2, times=2, code="99", message=self.SESSION_MSG))
        waits = []
        recorder = MemoryRecorder()
        client = TagoClient(FAKE_KEY, transport=fake.transport(), request_interval=0, retry_backoff=0,
                            sleep=waits.append, on_attempt=recorder, max_retries=0,
                            session_retries=3, session_retry_wait=90)
        recorder.start("stops", "k", service.STATIC_NORMALIZER_VERSION)
        collection = client.station_list(rows=2)
        self.assertEqual(collection.status, "success")
        self.assertEqual([(r.page_no, r.attempt_no, r.result_status, r.provider_code) for r in recorder.rows][1:4],
                         [(2, 1, "error", "99"), (2, 2, "error", "99"), (2, 3, "success", "00")])
        self.assertEqual(waits.count(90), 2, "세션 오류마다 90초 대기")
        self.assertEqual(client.request_count, len(recorder.rows), "대기 후 시도도 호출 수에 포함")

    def test_session_retry_is_bounded_and_other_99_is_not_retried(self):
        fake = FakeTago()
        fake.faults.append(Fault("getSttnNoList", "code", page=1, code="99", message=self.SESSION_MSG))
        client = client_for(fake, max_retries=0, session_retries=3, session_retry_wait=90)
        self.assertEqual(client.station_list(rows=2).status, "failed")
        self.assertEqual(fake.count(), 4, "최초 1회 + 세션 대기 재시도 3회")
        other = FakeTago()
        other.faults.append(Fault("getSttnNoList", "code", page=1, code="99", message="다른 오류"))
        client_for(other, max_retries=2, session_retries=3, session_retry_wait=90).station_list(rows=2)
        self.assertEqual(other.count(), 1, "다른 99 오류는 재시도하지 않음")
        default = FakeTago()
        default.faults.append(Fault("getSttnNoList", "code", page=1, code="99", message=self.SESSION_MSG))
        client_for(default, max_retries=2).station_list(rows=2)
        self.assertEqual(default.count(), 1, "옵션을 켜지 않으면 기존(Phase 1) 재시도 기준 그대로")

    def test_static_importer_enables_session_retry(self):
        seen = {}

        def factory(**kw):
            seen.update(kw)
            raise RuntimeError("stop")

        importer = service.StaticImportService(db=mock.MagicMock(), client_factory=factory)
        with mock.patch.object(service, "try_xact_lock", return_value=True), self.assertRaises(RuntimeError):
            importer.run()
        self.assertEqual((seen["session_retries"], seen["session_retry_wait"]), (3, 90.0))

    def test_existing_page_format_is_unchanged_without_callback(self):
        page = client_for(FakeTago()).fetch_page(service.bus_api.STATION_LIST, {"cityCode": "25"}, rows=2)
        self.assertEqual(set(vars(page.attempts[0])), {"started_at", "received_at", "outcome", "http_status"})


class StaticValidationTest(unittest.TestCase):
    def test_complete_collection_preserves_repeats_outside_stops_and_coordinates(self):
        (stops, routes, details, abort), recorder = collect_static(FakeTago())
        self.assertIsNone(abort)
        dataset, problems = service.build_static_dataset(stops, routes, details)
        self.assertEqual(problems, [])
        by_id = {s.external_id: s for s in dataset.stops}
        self.assertEqual(by_id["CCB0000009"].origin, "route_stops", "정류소 목록에 없는 시외 경유 정류소 보존")
        self.assertEqual((by_id["DJB0000005"].lat, by_id["DJB0000005"].lon), (None, None), "(0,0)은 좌표 미확인")
        self.assertEqual((by_id["DJB0000006"].lat, by_id["DJB0000006"].lon), (None, None), "누락 좌표")
        self.assertEqual(by_id["DJB0000002"].stop_no, "10002")
        r1 = [rs for rs in dataset.route_stops if rs.route_external_id == "DJB9000001"]
        self.assertEqual([(rs.seq, rs.stop_external_id, rs.direction_code) for rs in r1], [
            (1, "DJB0000001", "0"), (2, "DJB0000002", "0"), (3, "DJB0000003", "1"),
            (4, "DJB0000002", "1"), (5, "DJB0000001", "1")])
        self.assertEqual(len({rs.pattern_key for rs in r1}), 1, "노선 ID당 한 패턴")
        self.assertEqual(dataset.source["repeated_visit_entries"], 2)
        self.assertEqual(dataset.source["routes_with_direction_change"], 2)
        self.assertEqual(len(dataset.adopted_fetch_ids), len(recorder.rows), "재시도 없는 정상 수집은 모든 시도를 채택")

    def test_pattern_key_is_stable_and_changes_with_order(self):
        a = service.pattern_key("R", [("S1", "0"), ("S2", "0")])
        self.assertEqual(a, service.pattern_key("R", [("S1", "0"), ("S2", "0")]))
        self.assertNotEqual(a, service.pattern_key("R", [("S2", "0"), ("S1", "0")]))
        self.assertNotEqual(a, service.pattern_key("R", [("S1", "0"), ("S2", "1")]))
        self.assertTrue(a.startswith("p1:"))

    def test_missing_page_holds(self):
        fake = FakeTago()
        fake.faults.append(Fault("getSttnNoList", "empty_page", page=2))
        (stops, routes, details, abort), _ = collect_static(fake)
        self.assertEqual((stops.collection.status, stops.collection.stop_reason), ("partial", "empty_page_before_total"))
        self.assertEqual(fake.count("getRouteNoList"), 0, "정류소 목록이 불완전하면 이후 호출을 하지 않음")
        dataset, problems = service.build_static_dataset(stops, routes, details)
        self.assertIsNone(dataset)
        self.assertIn("collection_incomplete", [p.code for p in problems])

    def test_one_route_failure_holds_and_stops_remaining_calls(self):
        fake = FakeTago()
        fake.faults.append(Fault("getRouteAcctoThrghSttnList", "code", match={"routeId": "DJB9000002"}))
        (stops, routes, details, _), _ = collect_static(fake)
        self.assertEqual(fake.count("getRouteInfoIem"), 2, "실패한 노선 뒤의 노선은 호출하지 않음")
        dataset, problems = service.build_static_dataset(stops, routes, details)
        self.assertIsNone(dataset)
        codes = {p.code for p in problems}
        self.assertTrue({"route_stops_invalid", "not_collected"} <= codes)

    def test_conflicting_duplicate_station_is_not_merged(self):
        fake = FakeTago()
        dup = dict(fake.world["stations"][0], nodenm="다른이름")
        fake.world["stations"].append(dup)
        (stops, routes, details, _), _ = collect_static(fake)
        _, problems = service.build_static_dataset(stops, routes, details)
        self.assertIn("conflicting_duplicate", [p.code for p in problems])

    def test_duplicate_node_order_holds(self):
        fake = FakeTago()
        fake.world["routes"]["DJB9000003"]["stops"][1]["nodeord"] = 1
        (stops, routes, details, _), _ = collect_static(fake)
        _, problems = service.build_static_dataset(stops, routes, details)
        self.assertIn("duplicate_node_order", [p.code for p in problems])

    def test_coordinate_parsing(self):
        self.assertEqual(service.parse_coordinate("36.1", "127.2")[2], "valid")
        self.assertEqual(service.parse_coordinate(0, 0)[2], "zero")
        self.assertEqual(service.parse_coordinate(127.2, 36.1)[2], "out_of_range")
        self.assertEqual(service.parse_coordinate("x", 1)[2], "non_numeric")
        self.assertEqual(service.parse_coordinate(None, 1)[2], "missing")
        self.assertEqual(service.parse_coordinate(float("nan"), 1)[2], "non_numeric")

    def test_target_key_excludes_paging_and_secret(self):
        key = service.target_key(service.bus_api.ROUTE_STOPS, {"routeId": "R1", "cityCode": "25", "pageNo": "2",
                                                               "numOfRows": "100", "serviceKey": "x"})
        self.assertEqual(key, "tago:getRouteAcctoThrghSttnList?cityCode=25&routeId=R1")


class SchedulerTest(unittest.TestCase):
    KEY = "tago:getRouteAcctoBusLcList?cityCode=25&routeId=R1"

    def test_initial_fetch_is_immediate(self):
        now = datetime(2026, 11, 7, tzinfo=UTC)
        due = scheduler.next_due(self.KEY, 600, None, None, now)
        self.assertEqual((due.at, due.on_slot, due.reason), (now, False, "initial"))

    def test_never_earlier_than_interval_after_last_attempt(self):
        last = datetime(2026, 11, 7, 1, 2, 3, tzinfo=UTC)
        due = scheduler.next_due(self.KEY, 600, last, last + timedelta(seconds=1), last)
        self.assertGreaterEqual(due.at - last, timedelta(seconds=601))
        self.assertLess(due.at - last, timedelta(seconds=1201))
        self.assertAlmostEqual(((due.at - scheduler._EPOCH).total_seconds() - scheduler.phase_offset(self.KEY, 600))
                               % 600, 0, places=3)

    def test_consecutive_slots_are_exactly_one_interval_apart(self):
        slot = scheduler.first_slot_at_or_after(self.KEY, 600, datetime(2026, 11, 7, tzinfo=UTC))
        attempt = slot + timedelta(seconds=0.7)  # 실제 시도는 슬롯보다 조금 늦게 시작된다
        finished = attempt + timedelta(seconds=0.4)
        due = scheduler.next_due(self.KEY, 600, attempt, attempt + timedelta(seconds=0.2), attempt,
                                 ran_slot=(slot, finished))
        self.assertEqual(due.at - slot, timedelta(seconds=600), "지연이 누적돼 간격이 두 배가 되지 않음")
        other = scheduler.next_due(self.KEY, 600, attempt + timedelta(seconds=30), None, attempt,
                                   ran_slot=(slot, finished))
        self.assertGreaterEqual(other.at - (attempt + timedelta(seconds=30)), timedelta(seconds=600),
                                "다른 프로세스가 그 뒤에 조회했으면 간격을 다시 지킴")

    def test_phases_spread_targets_within_cycle(self):
        phases = {scheduler.phase_offset(f"tago:x?routeId=R{i}", 600) for i in range(50)}
        self.assertGreater(len({int(p // 60) for p in phases}), 5, "50개 대상이 한 구간에 몰리지 않음")


class ArrivalVisitMatchTest(unittest.TestCase):
    def test_unique_repeated_and_absent_visits(self):
        visits = [{"stop_external_id": "A", "pattern_key": "p", "seq": 1},
                  {"stop_external_id": "B", "pattern_key": "p", "seq": 2},
                  {"stop_external_id": "A", "pattern_key": "p", "seq": 3}]
        self.assertEqual(service._visit_match(visits, "B")[:2], ("p", 2))
        self.assertEqual(service._visit_match(visits, "A")[2]["status"], "ambiguous")
        self.assertEqual(service._visit_match(visits, "C")[2]["status"], "unmatched")


if __name__ == "__main__":
    unittest.main()
