"""ETA 오차 표본 수집 규칙의 DB 없는 시험: 운행 분리, 도착 이벤트 도출, 오차 표본, 설정·대상·대상별 주기.

위치 기록과 예측은 합성 값이다. 저장소 함수는 메모리 대역으로 바꿔 흐름(다시 실행해도 중복 없음)만 확인한다.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from pydantic import ValidationError

from app.core.config import Settings
from app.modules.evaluation.eta_errors import ArrivalPrediction, ObservedArrival, build_error_samples, horizon_bucket
from app.modules.transit import arrival_events as rules
from app.modules.transit import service
from app.modules.transit.arrival_events import RunPosition, VehicleState, derive_arrivals, next_run_group
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import realtime_target_key
from app.workers import scheduler
from app.workers.collection import collection_targets
from app.workflows import eta_error_sampling as flow

UTC = timezone.utc
T0 = datetime(2026, 11, 27, 0, 0, tzinfo=UTC)  # 09:00 KST


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def position(obs_id: int, seq: int | None, arr: float | None, collected: float | None = None,
             run: str = "run-1") -> RunPosition:
    """경유 순번 seq의 위치 기록. arr는 ARR_TIME, collected는 수집 시각(없으면 ARR_TIME + 20초)이다."""
    return RunPosition(obs_id, run, seq, None if arr is None else at(arr),
                       at((arr or 0) + 20 if collected is None else collected))


SAMPLING = dict(
    eta_error_sampling_enabled=True, eta_error_sampling_stops="DJB8001488,DJB8002695",
    eta_error_sampling_routes="DJB30300130", eta_error_sampling_interval_seconds=60,
    eta_error_sampling_hours="6-23", eta_error_match_window_seconds=1800,
    eta_error_horizon_edges_seconds="120,300,600", eta_error_run_gap_seconds=600,
)


def sampling_config(**overrides) -> flow.SamplingConfig:
    return flow.load_config(Settings(_env_file=None, **{**SAMPLING, **overrides}))


class RunSplitTest(unittest.TestCase):
    """운행 분리: 경유 순번이 줄거나 기록 간격이 설정값을 넘으면 새 운행이고, 정류소가 그대로면 저장하지 않는다."""

    COMMON = dict(route_external_id="DJB30300130", vehicle_no="대전75자1234", run_gap_seconds=600)

    def test_first_record_starts_a_run_and_next_stop_continues_it(self):
        first, started = next_run_group(None, node_id="8001000", stop_seq=10, collected_at=at(0), **self.COMMON)
        self.assertTrue(started)
        self.assertEqual(first, "dj:DJB30300130:대전75자1234:20261127T000000Z")
        state = VehicleState(first, "8001000", 10, at(0))
        self.assertEqual(next_run_group(state, node_id="8001001", stop_seq=11, collected_at=at(90), **self.COMMON),
                         (first, False))
        self.assertEqual(next_run_group(state, node_id="8009999", stop_seq=None, collected_at=at(90), **self.COMMON),
                         (first, False), "순번을 정하지 못한 정류소도 같은 운행에 남긴다")

    def test_unchanged_stop_is_not_stored_however_long_it_stays(self):
        state = VehicleState("run-1", "8001000", 10, at(0))
        for later in (60, 3600, 86400):
            self.assertEqual(next_run_group(state, node_id="8001000", stop_seq=10, collected_at=at(later),
                                            **self.COMMON), (None, False))

    def test_decreasing_order_or_long_gap_starts_another_run(self):
        state = VehicleState("run-1", "8001050", 50, at(0))
        back, started = next_run_group(state, node_id="8001002", stop_seq=2, collected_at=at(120), **self.COMMON)
        self.assertTrue(started, "순번이 줄면 다음 운행")
        self.assertNotEqual(back, "run-1")
        late, started = next_run_group(state, node_id="8001051", stop_seq=51, collected_at=at(601), **self.COMMON)
        self.assertTrue(started, "기록 간격이 설정값을 넘으면 다른 운행")
        self.assertNotEqual(late, "run-1")
        self.assertEqual(next_run_group(state, node_id="8001051", stop_seq=51, collected_at=at(600), **self.COMMON),
                         ("run-1", False), "설정값까지는 같은 운행")


class PositionRecordTest(unittest.TestCase):
    """대전시 노선 위치 항목(2026-11-27 실응답 형식)을 정류소가 바뀐 기록만 관측 행으로 만든다."""

    ROUTE, ROUTE_ID = "DJB30300130", 7
    NODES = ["8002485", "8002486", "8002487", "8002488", "8001940", "8001941"]

    @staticmethod
    def item(plate: str, node: str, arr_time: str = "20261127060612") -> dict[str, str]:
        return {"ARR_TIME": arr_time, "BUS_NODE_ID": node, "BUS_STOP_ID": "11360", "DIR": "0", "EVT_CD": "1",
                "GPS_LATI": "36.335552", "GPS_LONG": "127.449017", "PLATE_NO": plate, "ROUTE_CD": "30300130",
                "STRE_DT": arr_time, "TOTAL_DIST": "9004", "ud_type": "0"}

    def setUp(self):
        self.stored: dict[str, dict] = {}   # 차량번호 → 마지막 저장 기록(저장소 대역)
        visits = [{"pattern_key": "p1:608", "seq": seq, "source_node_order": str(seq),
                   "stop_external_id": f"DJB{node}", "stop_id": 100 + seq}
                  for seq, node in enumerate(self.NODES, start=1)]
        repo = service.repository
        patches = [
            mock.patch.object(repo, "route_ids", lambda conn, ids: {self.ROUTE: self.ROUTE_ID}),
            mock.patch.object(repo, "route_visits", lambda conn, ids: {self.ROUTE_ID: visits}),
            mock.patch.object(repo, "stop_ids", lambda conn, ids: {v["stop_external_id"]: v["stop_id"]
                                                                    for v in visits if v["stop_external_id"] in ids}),
            mock.patch.object(repo, "latest_daejeon_positions",
                              lambda conn, route_id, plates: {p: self.stored[p] for p in plates if p in self.stored}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def poll(self, items, seconds: float, fetch_id: int = 1):
        rows = service.normalize_daejeon_positions(None, self.ROUTE, "30300130", fetch_id, items, at(seconds), "v1",
                                                   run_gap_seconds=600)
        for row in rows:
            self.stored[row.vehicle_no] = {"run_group": row.details["run_group"],
                                           "node_id": row.details["source"]["BUS_NODE_ID"],
                                           "last_seq": row.details["run_last_seq"], "collected_at": row.collected_at}
        return rows

    def test_only_stop_changes_are_stored_with_a_run_key(self):
        first = self.poll([self.item("대전75자2719", "8002487"), self.item("대전75자2113", "8001940", "20261127060602")], 0)
        self.assertEqual([(r.vehicle_no, r.stop_seq, r.item_index) for r in first],
                         [("대전75자2719", 3, 0), ("대전75자2113", 5, 1)])
        row = first[0]
        self.assertEqual((row.kind, row.route_id, row.stop_id, row.pattern_key), ("position", 7, 103, "p1:608"))
        self.assertEqual((row.run_key, row.lat, row.lon, row.source_generated_at), (None, None, None, None),
                         "회차 키·좌표·원천 생성 시각은 채우지 않는다")
        details = row.details
        self.assertEqual((details["provider"], details["run_started"], details["run_last_seq"]), ("daejeon", True, 3))
        self.assertEqual(details["run_group"], "dj:DJB30300130:대전75자2719:20261127T000000Z")
        self.assertEqual(details["arr_time_at"], "2026-11-26T21:06:12+00:00", "ARR_TIME은 KST로 해석한다")
        self.assertEqual(details["source"]["GPS_LATI"], "36.335552", "원값은 보존")
        self.assertNotEqual(row.collected_at.isoformat(), details["arr_time_at"], "수집 시각과 ARR_TIME을 구분한다")

        same = self.poll([self.item("대전75자2719", "8002487", "20261127060700"),
                          self.item("대전75자2113", "8001940")], 60, fetch_id=2)
        self.assertEqual(same, [], "정류소가 그대로면 저장하지 않는다(ARR_TIME이 달라도)")

        moved = self.poll([self.item("대전75자2719", "8002488", "20261127060810"),
                           self.item("대전75자2113", "8001940")], 120, fetch_id=3)
        self.assertEqual([(r.vehicle_no, r.stop_seq, r.item_index) for r in moved], [("대전75자2719", 4, 0)])
        self.assertEqual((moved[0].details["run_group"], moved[0].details["run_started"]),
                         (details["run_group"], False), "같은 운행에 잇는다")

    def test_order_decrease_or_gap_starts_another_run_and_unknown_stops_keep_the_run(self):
        plate = "대전75자2719"
        run = self.poll([self.item(plate, "8002487")], 0)[0].details["run_group"]
        unknown = self.poll([self.item(plate, "9999999")], 60)[0]
        self.assertEqual((unknown.stop_seq, unknown.stop_id, unknown.details["run_group"],
                          unknown.details["run_last_seq"]), (None, None, run, 3), "순번 미확인 기록은 직전 순번을 이어 둔다")
        self.assertEqual(unknown.details["visit_match"]["status"], "unmatched")
        back = self.poll([self.item(plate, "8002485")], 120)[0]
        self.assertEqual((back.stop_seq, back.details["run_started"]), (1, True), "순번이 줄면 새 운행")
        self.assertNotEqual(back.details["run_group"], run)
        late = self.poll([self.item(plate, "8002486")], 120 + 601)[0]
        self.assertTrue(late.details["run_started"], "기록 간격이 설정값을 넘으면 새 운행")
        self.assertEqual(self.poll([{"PLATE_NO": "", "BUS_NODE_ID": "8002487"}, {"PLATE_NO": plate}], 800), [],
                         "차량이나 정류소를 알 수 없는 항목은 저장하지 않는다")

    def test_compact_kst_time_and_target_key(self):
        self.assertEqual(service.parse_compact_kst("20261127060612"), datetime(2026, 11, 26, 21, 6, 12, tzinfo=UTC))
        for bad in ("2026-11-27 06:06:12", "2026112706061", "20261327060612", "", None):
            self.assertIsNone(service.parse_compact_kst(bad), bad)
        target = RealtimeTarget("daejeon_positions", "DJB30300130")
        self.assertEqual(realtime_target_key(target), "daejeon:getBusPosByRtid?routeId=DJB30300130")
        self.assertEqual(service.daejeon_bus_route_id("DJB30300130"), "30300130")
        self.assertIsNone(service.daejeon_bus_route_id("30300130"))
        self.assertEqual(service.API_KIND["daejeon_positions"], "positions")
        self.assertTrue(realtime_target_key(RealtimeTarget("positions", "DJB30300130")).startswith("tago:"),
                        "TAGO 노선 위치 대상의 키는 그대로다")


class ArrivalEventTest(unittest.TestCase):
    """대상 경유 순번 20의 도착 이벤트. 19·20·21번 기록의 ARR_TIME은 각각 100·200·330초다."""

    FULL = [position(1, 19, 100), position(2, 20, 200), position(3, 21, 330)]

    def one(self, positions, meaning, targets=(20,)):
        derived = derive_arrivals(positions, targets, meaning)
        self.assertLessEqual(len(derived), 1)
        return derived[0] if derived else None

    def test_target_record_with_arrival_meaning_is_an_exact_time(self):
        event = self.one(self.FULL, "arrival")
        self.assertEqual((event.quality, event.event_at, event.basis), ("exact", at(200), "target_arr_time"))
        self.assertEqual((event.lower_at, event.upper_at), (at(200), at(200)))
        self.assertEqual(event.source_observation_ids, (2,))
        alone = self.one([position(2, 20, 200)], "arrival")
        self.assertEqual(alone.event_at, at(200), "점 시각은 앞뒤 기록이 없어도 쓴다")

    def test_departure_meaning_bounds_arrival_between_previous_and_target(self):
        event = self.one(self.FULL, "departure")
        self.assertEqual((event.quality, event.event_at, event.basis), ("bounded", None, "previous_to_target"))
        self.assertEqual((event.lower_at, event.upper_at), (at(100), at(200)))
        self.assertIsNone(self.one(self.FULL[1:], "departure"), "직전 기록이 없으면 하한을 알 수 없다")

    def test_missed_target_record_is_bounded_by_the_nearest_records(self):
        for meaning in rules.ARR_TIME_MEANINGS:
            event = self.one([self.FULL[0], self.FULL[2]], meaning)
            self.assertEqual((event.quality, event.event_at, event.basis), ("bounded", None, "previous_to_next"),
                             meaning)
            self.assertEqual((event.lower_at, event.upper_at), (at(100), at(330)))
        far = self.one([position(1, 15, 20), position(2, 17, 60), position(3, 24, 500), position(4, 26, 620)],
                       "arrival")
        self.assertEqual((far.lower_at, far.upper_at, far.lower_stop_seq, far.upper_stop_seq),
                         (at(60), at(500), 17, 24), "인접 정류소 기록이 없으면 가장 가까운 앞뒤 기록으로 범위를 넓힌다")

    def test_unverified_meaning_never_uses_the_target_time_as_a_point(self):
        event = self.one(self.FULL, "unverified")
        self.assertEqual((event.quality, event.event_at), ("bounded", None))
        self.assertEqual((event.lower_at, event.upper_at), (at(100), at(330)))
        self.assertEqual(event.source_observation_ids, (1, 2, 3))
        self.assertNotIn(at(200), (event.lower_at, event.upper_at), "대상 정류소의 ARR_TIME을 쓰지 않는다")
        self.assertNotEqual(event.collected_at, event.upper_at, "수집 시각은 도착 시각이 아니다")

    def test_no_event_without_records_on_both_sides(self):
        self.assertIsNone(self.one(self.FULL[:2], "unverified"), "다음 기록 없음")
        self.assertIsNone(self.one(self.FULL[1:], "unverified"), "직전 기록 없음")
        self.assertIsNone(self.one(self.FULL[:1], "arrival"), "대상 기록도 다음 기록도 없음")
        self.assertIsNone(self.one([], "arrival"))
        unusable = [position(1, 19, None), position(2, None, 200), position(3, 21, 330)]
        self.assertIsNone(self.one(unusable, "unverified"), "시각이나 순번을 모르는 기록은 근거로 쓰지 않는다")
        reversed_times = [position(1, 19, 400), position(3, 21, 330)]
        self.assertIsNone(self.one(reversed_times, "unverified"), "앞 기록이 더 늦으면 같은 운행으로 볼 수 없다")
        with self.assertRaises(ValueError):
            derive_arrivals(self.FULL, (20,), "guess")

    def test_runs_are_not_joined_and_keys_are_stable(self):
        """앞 운행의 19번 기록과 다음 운행의 21번 기록을 이어 20번 도착을 만들지 않는다."""
        split = [position(1, 19, 100, run="run-1"), position(3, 21, 330, run="run-2")]
        self.assertEqual(derive_arrivals(split, (20,), "unverified"), [])
        two = [*self.FULL, position(11, 19, 4000, run="run-2"), position(12, 21, 4200, run="run-2")]
        derived = derive_arrivals(two, (20,), "unverified")
        self.assertEqual([(event.run_group, event.lower_at, event.upper_at) for event in derived],
                         [("run-1", at(100), at(330)), ("run-2", at(4000), at(4200))])
        self.assertEqual(derived[0].event_key, "daejeon-position-arrival:run-1:20")
        again = derive_arrivals(list(reversed(two)), (20,), "arrival")
        self.assertEqual([event.event_key for event in again], [event.event_key for event in derived],
                         "입력 순서나 ARR_TIME 해석이 달라도 한 통과의 키는 같다")

    def test_stop_visited_twice_in_a_pattern_is_excluded(self):
        visits = [{"stop_external_id": stop, "pattern_key": "p", "seq": seq, "stop_id": 100 + seq}
                  for seq, stop in enumerate(["A", "B", "C", "B", "D"], start=1)]
        found, excluded = rules.target_positions(visits, ["A", "B", "D", "Z"])
        self.assertEqual({stop: visit["seq"] for stop, visit in found.items()}, {"A": 1, "D": 5})
        self.assertEqual(excluded, {"B": "repeated_visit", "Z": "stop_not_on_route"})


class ErrorSampleTest(unittest.TestCase):
    EDGES = (120.0, 300.0, 600.0)
    # 도착은 1000~1060초 사이다(범위).
    ARRIVAL = ObservedArrival(observation_id=900, run_group="run-1", quality="bounded", lower_at=at(1000),
                              upper_at=at(1060))

    @staticmethod
    def prediction(obs_id: int, basis: float, remaining: float, *, waiting: bool = False) -> ArrivalPrediction:
        return ArrivalPrediction(observation_id=obs_id, basis_at=at(basis),
                                 predicted_arrival_at=at(basis + remaining), basis_source="daejeon_info_offer_tm",
                                 waiting_departure=waiting)

    def build(self, predictions, arrival=None, window=1800):
        return build_error_samples(arrival or self.ARRIVAL, predictions, horizon_edges_seconds=self.EDGES,
                                   match_window_seconds=window)

    def test_horizon_buckets_include_the_lower_edge(self):
        self.assertEqual([horizon_bucket(value, self.EDGES) for value in (0, 119.9, 120, 299, 300, 599, 600, 5000)],
                         ["0-120", "0-120", "120-300", "120-300", "300-600", "300-600", "600+", "600+"])
        self.assertEqual(horizon_bucket(50, ()), "0+")

    def test_one_sample_per_run_and_bucket_from_the_earliest_prediction(self):
        predictions = [self.prediction(1, 300, 720), self.prediction(2, 360, 650),   # 600+ 구간 두 건
                       self.prediction(3, 520, 500), self.prediction(4, 700, 310),   # 300-600 구간 두 건
                       self.prediction(5, 780, 250), self.prediction(6, 960, 70)]    # 120-300, 0-120
        records = self.build(reversed(predictions))
        self.assertEqual([(r.horizon_bucket, r.prediction_observation_id) for r in records],
                         [("600+", 1), ("300-600", 3), ("120-300", 5), ("0-120", 6)])
        first = records[0]
        self.assertEqual((first.error_lower_seconds, first.error_upper_seconds), (-20.0, 40.0),
                         "오차 = 도착 하한·상한 − 예측 도착(1020초)")
        self.assertEqual((first.quality, first.eta_source, first.model_version, first.basis_source),
                         ("bounded", "provider", None, "daejeon_info_offer_tm"))
        self.assertEqual((first.run_group, first.event_observation_id, first.horizon_seconds), ("run-1", 900, 720.0))
        self.assertTrue(all(r.error_lower_seconds <= r.error_upper_seconds for r in records))

    def test_exact_arrival_gives_one_error_value(self):
        exact = ObservedArrival(900, "run-1", "exact", at(1000), at(1000))
        (record,) = self.build([self.prediction(1, 800, 230)], arrival=exact)
        self.assertEqual((record.quality, record.error_lower_seconds, record.error_upper_seconds),
                         ("exact", -30.0, -30.0))

    def test_not_departed_items_and_out_of_window_predictions_are_skipped(self):
        self.assertEqual(self.build([self.prediction(1, 300, 720, waiting=True)]), [], "MSG_TP=07은 예측이 아니다")
        self.assertEqual(self.build([self.prediction(1, 1061, 30)]), [], "도착 상한보다 늦은 기준 시각")
        self.assertEqual(len(self.build([self.prediction(1, 1060, 30)])), 1)
        self.assertEqual(self.build([self.prediction(1, 100, 900)], window=900), [], "기준 시각부터 도착까지 960초 > 900초")
        self.assertEqual(len(self.build([self.prediction(1, 160, 900)], window=900)), 1)
        kept = self.build([self.prediction(1, 100, 900), self.prediction(2, 400, 640)], window=900)
        self.assertEqual([r.prediction_observation_id for r in kept], [2], "창 밖의 더 이른 예측은 고르지 않는다")


class MemoryStore:
    """transit·evaluation 저장소 함수의 메모리 대역. 이벤트는 event_key로, 표본은 테이블 고유 키로 중복을 막는다."""

    ROUTE, ROUTE_ID, PATTERN = "DJB30300130", 7, "p1:608"
    STOPS = {"DJB8001487": 19, "DJB8001488": 20, "DJB8001489": 21}

    def __init__(self):
        self.positions: list[dict] = []
        self.predictions: list[dict] = []
        self.events: dict[str, dict] = {}
        self.samples: dict[tuple, dict] = {}
        self.table_ready = True

    def add_position(self, obs_id: int, seq: int, arr: float, run: str = "dj:DJB30300130:대전75자1234:run1"):
        self.positions.append({"id": obs_id, "route_id": self.ROUTE_ID, "pattern_key": self.PATTERN, "stop_seq": seq,
                               "vehicle_no": "대전75자1234", "collected_at": at(arr + 20), "run_group": run,
                               "arr_time_at": at(arr).isoformat()})

    def add_prediction(self, obs_id: int, basis: float, remaining: float, msg_tp: str = "03"):
        self.predictions.append({"id": obs_id, "basis_at": at(basis), "predicted_arrival_at": at(basis + remaining),
                                 "basis_source": "daejeon_info_offer_tm", "msg_tp": msg_tp})

    def patches(self):
        visits = [{"pattern_key": self.PATTERN, "seq": seq, "stop_external_id": stop, "stop_id": 100 + seq}
                  for stop, seq in self.STOPS.items()]

        def insert_event(conn, **row):
            if row["event_key"] in self.events:
                return None
            self.events[row["event_key"]] = row
            return 1000 + len(self.events)

        def insert_samples(conn, rows):
            created = 0
            for row in rows:
                key = (row["run_group"], row["eta_source"], row["model_version"] or "", row["horizon_bucket"])
                if key not in self.samples:
                    self.samples[key] = row
                    created += 1
            return created

        def predictions(conn, *, route_id, stop_id, vehicle_no, basis_from, basis_until):
            return [p for p in self.predictions if basis_from <= p["basis_at"] <= basis_until]

        transit, evaluation = flow.transit_repository, flow.evaluation_repository
        return [
            mock.patch.object(evaluation, "eta_error_samples_ready", lambda conn: self.table_ready),
            mock.patch.object(evaluation, "insert_eta_error_samples", insert_samples),
            mock.patch.object(transit, "route_ids", lambda conn, ids: {self.ROUTE: self.ROUTE_ID}),
            mock.patch.object(transit, "route_visits", lambda conn, ids: {self.ROUTE_ID: visits}),
            mock.patch.object(transit, "daejeon_positions_since",
                              lambda conn, ids, since: [p for p in self.positions if p["collected_at"] >= since]),
            mock.patch.object(transit, "existing_event_keys",
                              lambda conn, keys: {key for key in keys if key in self.events}),
            mock.patch.object(transit, "insert_arrival_event", insert_event),
            mock.patch.object(transit, "arrival_predictions", predictions),
        ]


class DeriveFlowTest(unittest.TestCase):
    def run_flow(self, store: MemoryStore, config: flow.SamplingConfig, now: datetime):
        patches = store.patches()
        for patch in patches:
            patch.start()
        try:
            return flow.derive_due(None, config, now)
        finally:
            for patch in patches:
                patch.stop()

    def filled_store(self) -> MemoryStore:
        store = MemoryStore()
        for obs_id, (seq, arr) in enumerate([(19, 900), (20, 1010), (21, 1100)], start=1):
            store.add_position(obs_id, seq, arr)
        store.add_prediction(51, 300, 720)                 # 600+
        store.add_prediction(52, 420, 600)                 # 600+ (같은 구간의 더 늦은 예측)
        store.add_prediction(53, 800, 230)                 # 120-300
        store.add_prediction(54, 850, 1500, msg_tp="07")   # 다음 차량 출발 전
        return store

    def test_events_and_samples_are_created_once(self):
        store, config = self.filled_store(), sampling_config(eta_error_sampling_stops="DJB8001488")
        first = self.run_flow(store, config, at(1200))
        self.assertEqual((first["status"], first["events_created"], first["samples_created"]), ("done", 1, 2))
        (event,) = store.events.values()
        self.assertEqual((event["quality"], event["event_at"], event["stop_seq"], event["stop_id"]),
                         ("bounded", None, 20, 120))
        self.assertEqual((event["details"]["bounded_lower_at"], event["details"]["bounded_upper_at"]),
                         (at(900).isoformat(), at(1100).isoformat()))
        self.assertEqual(event["details"]["arr_time_meaning"], "unverified")
        self.assertNotIn("run_key", event, "도출 이벤트에 회차 키를 넣지 않는다")
        self.assertEqual({(key[3], row["prediction_observation_id"]) for key, row in store.samples.items()},
                         {("600+", 51), ("120-300", 53)})
        sample = store.samples[(event["details"]["run_group"], "provider", "", "600+")]
        self.assertEqual((sample["error_lower_seconds"], sample["error_upper_seconds"], sample["quality"]),
                         (-120.0, 80.0, "bounded"))

        again = self.run_flow(store, config, at(1260))
        self.assertEqual((again["events_created"], again["samples_created"]), (0, 0), "다시 실행해도 중복 없음")
        self.assertEqual((len(store.events), len(store.samples)), (1, 2))

    def test_arrival_meaning_uses_the_target_time(self):
        store = self.filled_store()
        config = flow.load_config(Settings(_env_file=None, **{**SAMPLING, "eta_error_sampling_stops": "DJB8001488"},
                                           daejeon_position_arr_time_meaning="arrival"))
        self.run_flow(store, config, at(1200))
        (event,) = store.events.values()
        self.assertEqual((event["quality"], event["event_at"]), ("exact", at(1010)))
        sample = next(row for key, row in store.samples.items() if key[3] == "600+")
        self.assertEqual((sample["error_lower_seconds"], sample["error_upper_seconds"], sample["quality"]),
                         (-10.0, -10.0, "exact"))

    def test_one_sample_per_run_even_with_two_target_stops(self):
        store = self.filled_store()
        store.add_position(4, 22, 1190)
        result = self.run_flow(store, sampling_config(eta_error_sampling_stops="DJB8001488,DJB8001489"), at(1300))
        self.assertEqual(result["events_created"], 2, "대상 정류소마다 도착 이벤트는 만든다")
        self.assertEqual(len(store.samples), 2, "같은 운행의 같은 구간 표본은 먼저 도착한 정류소의 것 하나다")
        self.assertEqual({row["stop_seq"] for row in store.samples.values()}, {20})

    def test_missing_table_skips_without_creating_events(self):
        store = self.filled_store()
        store.table_ready = False
        result = self.run_flow(store, sampling_config(), at(1200))
        self.assertEqual(result, {"status": "skipped", "reason": "eta_error_samples_table_missing"})
        self.assertEqual(store.events, {})


class SamplingConfigTest(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(flow.load_config(Settings(_env_file=None)))
        self.assertIsNone(flow.load_config(Settings(_env_file=None, eta_error_sampling_enabled="")))
        off = {**SAMPLING, "eta_error_sampling_enabled": False}
        self.assertIsNone(flow.load_config(Settings(_env_file=None, **off)), "값이 있어도 꺼져 있으면 수집하지 않는다")

    def test_enabled_without_required_values_is_a_config_error(self):
        with self.assertRaises(flow.SamplingConfigError) as raised:
            flow.load_config(Settings(_env_file=None, eta_error_sampling_enabled=True,
                                      eta_error_sampling_stops="DJB8001488"))
        message = str(raised.exception)
        for name in ("ROUTES", "INTERVAL_SECONDS", "HOURS"):
            self.assertIn(f"BACKEND_ETA_ERROR_SAMPLING_{name}", message)
        for name in ("MATCH_WINDOW_SECONDS", "HORIZON_EDGES_SECONDS", "RUN_GAP_SECONDS"):
            self.assertIn(f"BACKEND_ETA_ERROR_{name}", message)
        self.assertNotIn("BACKEND_ETA_ERROR_SAMPLING_STOPS", message)
        self.assertNotIn("DAILY_CALL_LIMIT", message, "일일 호출 상한은 선택 값이다")
        with self.assertRaises(flow.SamplingConfigError) as raised:
            sampling_config(eta_error_run_gap_seconds=60)
        self.assertIn("BACKEND_ETA_ERROR_RUN_GAP_SECONDS", str(raised.exception))

    def test_values_are_parsed_and_checked(self):
        config = sampling_config(eta_error_sampling_hours="7-9, 17-20", eta_error_sampling_daily_call_limit="10000")
        self.assertEqual((config.stop_ids, config.route_ids), (("DJB8001488", "DJB8002695"), ("DJB30300130",)))
        self.assertEqual((config.hours, config.horizon_edges_seconds, config.daily_call_limit),
                         (((7, 9), (17, 20)), (120.0, 300.0, 600.0), 10000))
        self.assertEqual(config.arr_time_meaning, "unverified", "ARR_TIME의 뜻은 확인 전이 기본이다")
        for name, bad in (("eta_error_sampling_hours", "23-6"), ("eta_error_sampling_hours", "6"),
                          ("eta_error_sampling_hours", "6-25"), ("eta_error_horizon_edges_seconds", "300,120"),
                          ("eta_error_horizon_edges_seconds", "0,120"), ("eta_error_sampling_interval_seconds", "0"),
                          ("daejeon_position_arr_time_meaning", "guess")):
            with self.assertRaises(ValidationError, msg=(name, bad)):
                Settings(_env_file=None, **{name: bad})

    def test_hours_are_kst(self):
        config = sampling_config(eta_error_sampling_hours="6-23")
        kst = timezone(timedelta(hours=9))
        self.assertTrue(config.in_hours(datetime(2026, 11, 27, 6, 0, tzinfo=kst)))
        self.assertTrue(config.in_hours(datetime(2026, 11, 27, 22, 59, tzinfo=kst)))
        self.assertFalse(config.in_hours(datetime(2026, 11, 27, 23, 0, tzinfo=kst)), "끝 시각은 제외")
        self.assertFalse(config.in_hours(datetime(2026, 11, 27, 5, 59, tzinfo=kst)))
        self.assertTrue(config.in_hours(datetime(2026, 11, 26, 21, 30, tzinfo=UTC)), "UTC 21:30 = KST 06:30")


class SamplingScheduleTest(unittest.TestCase):
    INSIDE = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)    # 10:00 KST
    OUTSIDE = datetime(2026, 11, 26, 15, 30, tzinfo=UTC)  # 00:30 KST

    def test_no_sampling_targets_outside_the_hours(self):
        config = sampling_config()
        plan = flow.collection_plan(None, config, self.OUTSIDE)
        self.assertEqual((plan.status, plan.targets, plan.intervals), ("outside_hours", (), {}))
        targets = collection_targets(Settings(_env_file=None), extra=plan.targets)
        self.assertEqual(targets, [], "시간대 밖에서는 표본 수집 대상이 없어 호출하지 않는다")

    def test_targets_and_short_interval_inside_the_hours(self):
        plan = flow.collection_plan(None, sampling_config(), self.INSIDE)
        self.assertEqual(plan.status, "active")
        self.assertEqual({(t.kind, t.external_id) for t in plan.targets},
                         {("arrivals", "DJB8001488"), ("arrivals", "DJB8002695"),
                          ("daejeon_positions", "DJB30300130")})
        self.assertEqual(plan.intervals, {
            "daejeon:getArrInfoByStopID?nodeId=DJB8001488": 60.0,
            "daejeon:getArrInfoByStopID?nodeId=DJB8002695": 60.0,
            "daejeon:getBusPosByRtid?routeId=DJB30300130": 60.0})
        self.assertTrue(all(t.sources == frozenset({"eta_error_sampling"}) for t in plan.targets))
        settings = Settings(_env_file=None, realtime_arrival_stop_ids="DJB8001488,DJB9000001")
        merged = {(t.kind, t.external_id): t.sources for t in collection_targets(settings, extra=plan.targets)}
        self.assertEqual(merged[("arrivals", "DJB8001488")], frozenset({"dev_config", "eta_error_sampling"}),
                         "다른 수요로도 등록된 정류소는 한 대상으로 합친다")
        self.assertEqual(len(merged), 4)

    def test_only_sampling_targets_use_the_short_interval(self):
        """표본 수집 대상은 60초, 나머지는 600초. 마지막 조회가 100초 전이면 표본 수집 대상만 지금 조회한다."""
        now = self.INSIDE
        plan = flow.collection_plan(None, sampling_config(), now)
        regular = [RealtimeTarget("arrivals", "DJB9000001", frozenset({"route_search:1"})),
                   RealtimeTarget("positions", "DJB30300001", frozenset({"congestion"}))]
        targets = collection_targets(Settings(_env_file=None), extra=[*plan.targets, *regular])
        last = now - timedelta(seconds=100)
        calls = []

        def timings(conn, api_kind, keys):
            calls.append((api_kind, sorted(keys)))
            return {key: (last, last) for key in keys}

        with mock.patch.object(scheduler.repository, "target_timings", side_effect=timings):
            plans = scheduler.plan_realtime(None, targets, 600, now, {}, plan.intervals)
        by_key = {p.target_key: p for p in plans}
        for key in plan.intervals:
            self.assertEqual(by_key[key].interval_seconds, 60.0)
            self.assertLessEqual(by_key[key].due.at, now, key)
            self.assertGreaterEqual(by_key[key].due.at - last, timedelta(seconds=60), "주기보다 일찍 조회하지 않는다")
        for target in regular:
            item = by_key[realtime_target_key(target)]
            self.assertEqual(item.interval_seconds, 600)
            self.assertGreaterEqual(item.due.at - last, timedelta(seconds=600), "나머지 대상은 600초를 지킨다")
            self.assertGreater(item.due.at, now)
        self.assertEqual({kind for kind, _ in calls}, {"arrivals", "positions"},
                         "대전시 노선 위치의 호출 기록 종류는 positions다")
        positions_keys = next(keys for kind, keys in calls if kind == "positions")
        self.assertEqual(len(positions_keys), 2, "TAGO 위치와 대전시 위치를 함께 조회한다")

    def test_short_interval_target_does_not_replay_missed_slots(self):
        """표본 수집 대상이 한참 뒤에 다시 대상이 돼도(다음 날 수집 시간대 시작) 밀린 슬롯을 연달아 조회하지 않는다."""
        key = "daejeon:getBusPosByRtid?routeId=DJB30300130"
        last = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)
        clock = datetime(2026, 11, 26, 21, 0, 5, tzinfo=UTC)
        ran_slots, calls = {}, 0
        for _ in range(50):
            due = scheduler.next_due(key, 60, last, last, clock, ran_slot=ran_slots.get(key))
            if due.at > clock:
                break
            calls += 1
            last, finished = clock + timedelta(seconds=0.1), clock + timedelta(seconds=0.4)
            if due.on_slot:
                ran_slots[key] = (due.at, finished)
            clock = finished
        self.assertEqual(calls, 1)
        self.assertLessEqual(due.at - clock, timedelta(seconds=60))

    def test_daily_call_limit_drops_only_the_exhausted_service(self):
        config = sampling_config(eta_error_sampling_daily_call_limit=10000)
        counts = {"arrivals": 10000, "positions": 9999}
        seen = []

        def count(conn, provider, since, api_kind=None):
            seen.append((provider, since, api_kind))
            return counts[api_kind]

        with mock.patch.object(flow.transit_repository, "provider_call_count", side_effect=count):
            plan = flow.collection_plan(object(), config, self.INSIDE)
            self.assertEqual((plan.status, plan.limited), ("active", {"arrivals": 10000}))
            self.assertEqual([t.kind for t in plan.targets], ["daejeon_positions"])
            counts["positions"] = 12000
            stopped = flow.collection_plan(object(), config, self.INSIDE)
        self.assertEqual((stopped.status, stopped.targets, stopped.intervals),
                         ("daily_call_limit_reached", (), {}))
        self.assertEqual({item[0] for item in seen}, {"daejeon"})
        self.assertEqual({item[1] for item in seen}, {datetime(2026, 11, 26, 15, 0, tzinfo=UTC)},
                         "그날(KST) 0시부터 센다")
        unlimited = flow.collection_plan(object(), sampling_config(), self.INSIDE)
        self.assertEqual(len(unlimited.targets), 3, "상한을 비우면 제한하지 않는다")


if __name__ == "__main__":
    unittest.main()
