"""ETA 오차 표본 수집 격리 시험: 임시 PostgreSQL/PostGIS에서 워커 수집 → 위치 기록 → 도착 이벤트 → 오차 표본 → 현황 조회.

    $env:TEST_POSTGIS_DSN = "postgresql://postgres:...@127.0.0.1:55439/postgres"
    uv run python -m unittest tests.integration.test_eta_error_db

대전시 응답은 합성 값이다(형식은 2026-11-27 실응답). 실제 외부 호출은 없다. 합성 정적 자료에 대전시 형식 ID('DJB'+숫자)의
노선 하나와 정류소 다섯 곳을 더해 쓴다.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))

import test_phase4_api as base  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.integrations.daejeon_bus_api import DaejeonBusClient  # noqa: E402
from app.modules.congestion import repository as congestion_repository  # noqa: E402
from app.modules.evaluation import repository as evaluation_repository  # noqa: E402
from app.modules.transit import repository as transit_repository  # noqa: E402
from app.workers.main import Worker, describe_targets  # noqa: E402
from app.workflows import search as flow  # noqa: E402
from scripts import eta_error_status  # noqa: E402

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
FAKE_KEY = "integration-daejeon-key+/=="
ROUTE = "DJB30300130"
NODES = ["8002485", "8002486", "8002487", "8002488", "8002489"]  # 경유 순번 1~5
PATTERN = "p1:eta-error-608"
PLATE = "대전75자2719"
TARGET_STOP = f"DJB{NODES[2]}"  # 경유 순번 3
POSITIONS_KEY = f"daejeon:getBusPosByRtid?routeId={ROUTE}"
ARRIVALS_KEY = f"daejeon:getArrInfoByStopID?nodeId={TARGET_STOP}"
SAMPLING = dict(
    eta_error_sampling_enabled=True, eta_error_sampling_stops=TARGET_STOP, eta_error_sampling_routes=ROUTE,
    eta_error_sampling_interval_seconds=60, eta_error_sampling_hours="0-24", eta_error_match_window_seconds=1800,
    eta_error_horizon_edges_seconds="120,300,600", eta_error_run_gap_seconds=600,
)
HEADER = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><ServiceResult><msgHeader><currentPage>1"
          "</currentPage><headerCd>{code}</headerCd><headerMsg>{message}</headerMsg><itemCnt>{count}</itemCnt>"
          "<itemPageCnt>1</itemPageCnt></msgHeader>")
GATEWAY = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR serviceKey=" + FAKE_KEY + "</errMsg>"
           "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
           "<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")


def items_xml(items: list[dict[str, str]]) -> str:
    body = "".join("<itemList>" + "".join(f"<{k}>{v}</{k}>" for k, v in item.items()) + "</itemList>"
                   for item in items)
    return (HEADER.format(code="0", message="정상적으로 처리되었습니다.", count=len(items))
            + f"<msgBody>{body}</msgBody></ServiceResult>")


def no_result_xml() -> str:
    return HEADER.format(code="4", message="결과가 없습니다.", count=0) + "<msgBody/></ServiceResult>"


def bus(node: str, arr_time: datetime, plate: str = PLATE) -> dict[str, str]:
    stamp = arr_time.astimezone(KST).strftime("%Y%m%d%H%M%S")
    return {"ARR_TIME": stamp, "BUS_NODE_ID": node, "BUS_STOP_ID": "11360", "DIR": "0", "EVT_CD": "1",
            "GPS_LATI": "36.335552", "GPS_LONG": "127.449017", "PLATE_NO": plate, "ROUTE_CD": ROUTE[3:],
            "STRE_DT": stamp, "TOTAL_DIST": "9004", "ud_type": "0"}


def arrival(plate: str, remaining: int, offered_at: datetime, msg_tp: str = "03") -> dict[str, str]:
    return {"BUS_NODE_ID": NODES[2], "BUS_STOP_ID": "11380", "CAR_REG_NO": plate, "EXTIME_SEC": str(remaining),
            "INFO_OFFER_TM": offered_at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"), "MSG_TP": msg_tp,
            "ROUTE_CD": ROUTE[3:], "ROUTE_NO": "608", "STATUS_POS": "1"}


class FakeDaejeon:
    """합성 대전시 서버. buses는 노선 위치 응답, arrivals는 정류소 도착정보 응답의 항목이다."""

    def __init__(self):
        self.buses: list[dict[str, str]] = []
        self.arrivals: list[dict[str, str]] = []
        self.position_error = False
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"content-type": "application/xml;charset=UTF-8"}
        if request.url.path.endswith("/getBusPosByRtid"):
            if self.position_error:
                return httpx.Response(429, text=GATEWAY, headers=headers)
            return httpx.Response(200, text=items_xml(self.buses) if self.buses else no_result_xml(), headers=headers)
        return httpx.Response(200, text=items_xml(self.arrivals), headers=headers)

    def calls(self, operation: str) -> int:
        return sum(1 for request in self.requests if request.url.path.endswith(f"/{operation}"))


class EtaErrorSamplingDbTest(base.Phase4TestCase):
    def setUp(self):
        super().setUp()
        version = self.query("SELECT DISTINCT data_version FROM app.routes WHERE is_active")[0][0]
        self.route_id = self.query(
            "INSERT INTO app.routes (provider, external_id, route_no, route_type, start_name, end_name, data_version, "
            "updated_at) VALUES ('tago', %s, '608', '간선버스', '기점', '종점', %s, now()) RETURNING id",
            (ROUTE, version))[0][0]
        self.stop_ids = {}
        for seq, node in enumerate(NODES, start=1):
            stop_id = self.query(
                "INSERT INTO app.stops (provider, external_id, name, stop_no, data_version, updated_at) "
                "VALUES ('tago', %s, %s, %s, %s, now()) RETURNING id",
                (f"DJB{node}", f"합성정류소{seq}", str(11350 + seq * 10), version))[0][0]
            self.stop_ids[seq] = stop_id
            self.query("INSERT INTO app.route_stops (route_id, pattern_key, direction_code, seq, source_node_order, "
                       "stop_id, data_version) VALUES (%s, %s, '0', %s, %s, %s, %s) RETURNING id",
                       (self.route_id, PATTERN, seq, str(seq), stop_id, version))
        self.daejeon = FakeDaejeon()
        self.offset = timedelta(0)
        self.use_settings()

    def use_settings(self, **extra):
        self.settings = Settings(_env_file=None, **base.SETTINGS, **self.boundary_setting(),
                                 **{**SAMPLING, **extra})

    def sampling_worker(self) -> Worker:
        return Worker(
            settings_factory=lambda: self.settings, db_factory=lambda _s: self.db,
            client_factory=lambda _s, **kw: self.tago_client(**kw),
            daejeon_client_factory=lambda _s: DaejeonBusClient(FAKE_KEY, transport=httpx.MockTransport(
                self.daejeon.handle)),
            clock=lambda: datetime.now(UTC) + self.offset, static_enabled=False, jobs_enabled=False)

    def fetches(self, key: str):
        return self.query("SELECT provider, api_kind, result_status, provider_code, raw_body IS NULL, "
                          "published_at IS NOT NULL FROM app.api_fetches WHERE target_key = %s ORDER BY id", (key,))

    def test_worker_collects_positions_derives_arrivals_and_builds_error_samples(self):
        now = datetime.now(UTC).replace(microsecond=0)
        worker = self.sampling_worker()

        # ---- 1회차: 차량이 대상 정류소(순번 3) 직전(순번 2)에 있고, 대상 정류소 도착 예측이 있다.
        self.daejeon.buses = [bus(NODES[1], now - timedelta(seconds=300))]
        self.daejeon.arrivals = [
            arrival(PLATE, 120, now - timedelta(seconds=280)),                      # 예측 도착 = now − 160초
            arrival("대전75자9999", 1650, now - timedelta(seconds=900), msg_tp="07"),  # 다음 차량 출발 전(예측 아님)
        ]
        first = worker.run_pass()
        self.assertEqual(first.targets, 2)
        self.assertEqual((first.eta_errors["status"], first.eta_errors["targets"]), ("active", 2))
        self.assertEqual(first.eta_errors["derived"], {"status": "done", "events_created": 0, "samples_created": 0,
                                                       "excluded_stops": {}})
        self.assertEqual({o["target_key"]: o["action"] for o in first.outcomes},
                         {POSITIONS_KEY: "published", ARRIVALS_KEY: "published"})
        self.assertEqual(self.fetches(POSITIONS_KEY), [("daejeon", "positions", "success", "0", True, True)],
                         "정상 응답의 원본 본문은 남기지 않는다")
        self.assertEqual(self.fetches(ARRIVALS_KEY), [("daejeon", "arrivals", "success", "0", True, True)])
        sent = next(r for r in self.daejeon.requests if r.url.path.endswith("/getBusPosByRtid"))
        self.assertEqual((sent.url.path, sent.url.params["busRouteId"]),
                         ("/6300000/busposinfo/getBusPosByRtid", "30300130"))

        # ---- 2회차: 대상 정류소를 지나 순번 4에 있다(대상 정류소 기록은 놓침). 도착은 앞뒤 기록 사이의 범위다.
        self.offset = timedelta(seconds=130)
        self.daejeon.buses = [bus(NODES[3], now - timedelta(seconds=100))]
        self.daejeon.arrivals = []
        second = worker.run_pass()
        self.assertEqual((second.eta_errors["derived"]["events_created"],
                          second.eta_errors["derived"]["samples_created"]), (1, 1))
        positions = self.query(
            "SELECT stop_seq, stop_id, vehicle_no, run_key, location IS NULL, details ->> 'run_group', "
            "details ->> 'provider', details ->> 'arr_time_at' FROM app.transit_observations "
            "WHERE kind = 'position' AND route_id = %s ORDER BY id", (self.route_id,))
        self.assertEqual([(row[0], row[1], row[2], row[3], row[4], row[6]) for row in positions],
                         [(2, self.stop_ids[2], PLATE, None, True, "daejeon"),
                          (4, self.stop_ids[4], PLATE, None, True, "daejeon")])
        run_group = positions[0][5]
        self.assertEqual(positions[1][5], run_group, "같은 운행")
        lower, upper = now - timedelta(seconds=300), now - timedelta(seconds=100)
        events = self.query(
            "SELECT id, event_type, quality, event_at, run_key, stop_seq, stop_id, vehicle_no, pattern_key, event_key, "
            "details FROM app.transit_observations WHERE kind = 'event'")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event[1:9], ("arrival", "bounded", None, None, 3, self.stop_ids[3], PLATE, PATTERN))
        self.assertEqual(event[9], f"daejeon-position-arrival:{run_group}:3")
        self.assertEqual((event[10]["bounded_lower_at"], event[10]["bounded_upper_at"], event[10]["basis"],
                          event[10]["arr_time_meaning"]),
                         (lower.isoformat(), upper.isoformat(), "previous_to_next", "unverified"))
        samples = self.query(
            "SELECT run_group, eta_source, model_version, basis_source, horizon_bucket, horizon_seconds, "
            "error_lower_seconds, error_upper_seconds, quality, route_id, stop_id, stop_seq, vehicle_no, "
            "arrival_lower_at, arrival_upper_at, prediction_observation_id, event_observation_id "
            "FROM app.eta_error_samples")
        self.assertEqual(len(samples), 1, "MSG_TP=07 항목은 표본이 되지 않는다")
        sample = samples[0]
        self.assertEqual(sample[:9], (run_group, "provider", None, "daejeon_info_offer_tm", "120-300", 120.0, -140.0,
                                      60.0, "bounded"))
        self.assertEqual(sample[9:15], (self.route_id, self.stop_ids[3], 3, PLATE, lower, upper))
        self.assertEqual(sample[16], event[0])
        self.assertEqual(self.query("SELECT kind, vehicle_no, remaining_seconds FROM app.transit_observations "
                                    "WHERE id = %s", (sample[15],)), [("arrival", PLATE, 120)])

        # ---- 3회차: 정류소가 그대로면 위치 기록도 이벤트도 표본도 늘지 않는다.
        self.offset = timedelta(seconds=260)
        third = worker.run_pass()
        self.assertEqual((third.eta_errors["derived"]["events_created"],
                          third.eta_errors["derived"]["samples_created"]), (0, 0))
        self.assertEqual(self.query(
            "SELECT (SELECT count(*) FROM app.transit_observations WHERE kind = 'position' AND route_id = %s), "
            "(SELECT count(*) FROM app.transit_observations WHERE kind = 'event'), "
            "(SELECT count(*) FROM app.eta_error_samples)", (self.route_id,)), [(2, 1, 1)])
        self.assertEqual(len(self.fetches(POSITIONS_KEY)), 3, "조회는 했지만 바뀐 차량이 없어 빈 공개다")
        self.assertEqual(self.daejeon.calls("getBusPosByRtid"), 3)

        # ---- 중복 방지와 제약: 같은 이벤트 키·같은 표본 키는 다시 들어가지 않는다.
        with self.db.transaction() as conn:
            self.assertIsNone(transit_repository.insert_arrival_event(
                conn, event_key=event[9], quality="exact", event_at=now, collected_at=now, route_id=self.route_id,
                stop_id=self.stop_ids[3], pattern_key=PATTERN, stop_seq=3, vehicle_no=PLATE, details={}))
            row = {"run_group": run_group, "eta_source": "provider", "model_version": None, "basis_source": None,
                   "horizon_bucket": "120-300", "horizon_seconds": 200.0, "error_lower_seconds": 1.0,
                   "error_upper_seconds": 1.0, "quality": "exact", "route_id": self.route_id,
                   "stop_id": self.stop_ids[3], "pattern_key": PATTERN, "stop_seq": 3, "vehicle_no": PLATE,
                   "basis_at": now, "predicted_arrival_at": now + timedelta(seconds=200), "arrival_lower_at": now,
                   "arrival_upper_at": now, "prediction_observation_id": None, "event_observation_id": None}
            self.assertEqual(evaluation_repository.insert_eta_error_samples(conn, [row]), 0, "같은 운행·구간")
            self.assertEqual(evaluation_repository.insert_eta_error_samples(
                conn, [{**row, "horizon_bucket": "300-600", "horizon_seconds": 400.0}]), 1)
            self.assertEqual(evaluation_repository.insert_eta_error_samples(
                conn, [{**row, "model_version": "other-model"}]), 1, "모델 버전이 다르면 다른 표본")
        for change in ({"quality": "guess"}, {"error_lower_seconds": 5.0, "error_upper_seconds": 1.0},
                       {"quality": "exact", "error_lower_seconds": 1.0, "error_upper_seconds": 2.0},
                       {"error_upper_seconds": float("nan")}, {"horizon_seconds": -1.0}):
            with self.subTest(change=change), self.assertRaises(psycopg.errors.CheckViolation):
                with self.db.transaction() as conn:
                    evaluation_repository.insert_eta_error_samples(
                        conn, [{**row, "run_group": "other-run", **change}])

        # ---- 기존 조회에는 섞이지 않는다: 회차 키가 있는 이벤트만 읽는 구간 소요시간, TAGO 위치만 세는 혼잡도 통과 횟수.
        with self.db.transaction() as conn:
            self.assertEqual(transit_repository.arrival_events(conn, [PATTERN]), [])
            _, points = congestion_repository.position_history(
                conn, self.route_id, PATTERN, "tago:getRouteAcctoBusLcList?cityCode=25&routeId=" + ROUTE,
                now - timedelta(days=1), now + timedelta(days=1))
            self.assertEqual(points, [])

        # ---- 확률 계산용 읽기와 현황 조회.
        policy = flow.load_search_config(Settings(
            _env_file=None, **base.SETTINGS, eval_marginal_probability_enabled=True, eval_marginal_window_days=14,
            eval_min_marginal_samples=1, eta_error_horizon_edges_seconds="120,300,600")).evaluation_policy
        with self.db.transaction() as conn:
            loaded = flow.marginal_error_samples(conn, policy, datetime.now(UTC) + timedelta(minutes=1))
            self.assertEqual(flow.marginal_error_samples(conn, policy, now - timedelta(days=1)), [],
                             "평가 기준시각 뒤에 만들어진 표본은 읽지 않는다")
        self.assertEqual(sorted((s.horizon_bucket, s.model_version, s.error_lower_seconds, s.error_upper_seconds)
                                for s in loaded if s.run_group_key == run_group and s.model_version is None),
                         [("120-300", None, -140.0, 60.0), ("300-600", None, 1.0, 1.0)])
        status = eta_error_status.report(self.settings, self.db, None)
        self.assertTrue(status["table_ready"])
        self.assertEqual((status["samples"]["total"], status["samples"]["independent_runs"]), (3, 1))
        group = next(g for g in status["groups"] if g["horizon_bucket"] == "120-300" and g["model_version"] is None)
        self.assertEqual((group["samples"], group["by_quality"], group["error_seconds"]["exact"]),
                         (1, {"exact": 0, "bounded": 1}, None))
        self.assertEqual((group["error_seconds"]["lower"]["median"], group["error_seconds"]["upper"]["median"],
                          group["bounded_width_seconds"]["median"]), (-140.0, 60.0, 200.0))
        self.assertEqual((status["collection"]["position_records"]["count"],
                          status["collection"]["position_records"]["runs"],
                          status["collection"]["arrival_events"]["by_quality"]), (2, 1, {"bounded": 1}))
        described = describe_targets(self.settings, self.db)
        self.assertEqual(described["eta_error_sampling"]["status"], "active")
        self.assertEqual({(t["kind"], t["interval_seconds"]) for t in described["targets"]},
                         {("daejeon_positions", 60.0), ("arrivals", 60.0)})

    def test_empty_and_error_responses(self):
        worker = self.sampling_worker()
        self.daejeon.buses = []   # 운행 차량 없음: headerCd=4
        worker.run_pass()
        self.assertEqual(self.fetches(POSITIONS_KEY), [("daejeon", "positions", "empty", "4", True, True)],
                         "운행 차량이 없는 응답은 오류가 아니라 빈 공개다")
        self.daejeon.position_error = True
        self.offset = timedelta(seconds=130)
        failed = worker.run_pass()
        self.assertEqual(self.fetches(POSITIONS_KEY)[-1], ("daejeon", "positions", "error", "22", False, False),
                         "오류 응답은 본문을 남기고 공개하지 않는다")
        body = self.query("SELECT raw_body, error_reason FROM app.api_fetches WHERE target_key = %s ORDER BY id DESC "
                          "LIMIT 1", (POSITIONS_KEY,))[0]
        self.assertNotIn(FAKE_KEY, body[0])
        self.assertTrue(body[1].startswith("gateway: 22"))
        self.assertEqual(next(o["action"] for o in failed.outcomes if o["target_key"] == POSITIONS_KEY), "failed")
        self.assertEqual(self.query("SELECT count(*) FROM app.transit_observations WHERE kind = 'position' "
                                    "AND route_id = %s", (self.route_id,)), [(0,)])

        # 한도 초과로 거절된 날에는 상한 설정이 없어도 표본 수집 호출을 멈춘다(도착정보 쪽도 함께).
        calls = len(self.daejeon.requests)
        self.offset = timedelta(seconds=260)
        stopped = worker.run_pass()
        self.assertEqual((stopped.targets, stopped.eta_errors["status"], stopped.eta_errors["daily_call_limit_reached"]),
                         (0, "daily_call_limit_reached", {"positions": 2}))
        self.assertEqual(len(self.daejeon.requests), calls)

    def test_tago_source_keeps_every_snapshot_and_bounds_the_arrival_by_query_times(self):
        """노선 위치 원천이 TAGO면 대전시 노선 위치를 부르지 않고, 도착을 앞뒤 조회 시각 사이의 범위로 남긴다."""
        self.use_settings(eta_error_position_source="tago")
        worker = self.sampling_worker()
        key = "tago:getRouteAcctoBusLcList?cityCode=25&routeId=" + ROUTE
        now = datetime.now(UTC).replace(microsecond=0)

        def tago_bus(order: int) -> dict:
            return {"routenm": 608, "routetp": "간선버스", "nodeid": f"DJB{NODES[order - 1]}",
                    "nodenm": f"합성정류소{order}", "nodeord": order, "vehicleno": PLATE,
                    "gpslati": 36.335552, "gpslong": 127.449017}

        # ---- 1·2회차: 차량이 대상 정류소(순번 3) 직전(순번 2)에 머문다. 조회마다 행을 남긴다.
        self.tago.world["positions"][ROUTE] = [tago_bus(2)]
        self.daejeon.arrivals = [arrival(PLATE, 120, now - timedelta(seconds=30))]   # 예측 도착 = now + 90초
        first = worker.run_pass()
        self.assertEqual({o["target_key"]: o["action"] for o in first.outcomes},
                         {key: "published", ARRIVALS_KEY: "published"})
        self.assertEqual(self.fetches(key), [("tago", "positions", "success", "00", True, True)],
                         "표본 수집 대상의 정상 응답은 원본 본문을 남기지 않는다")
        self.offset = timedelta(seconds=130)
        self.daejeon.arrivals = []
        second = worker.run_pass()
        self.assertEqual(second.eta_errors["derived"]["events_created"], 0)

        # ---- 3회차: 대상 정류소를 지나 순번 4에 있다. 도착은 (2회차 조회를 보낸 시각, 3회차 응답을 받은 시각] 범위다.
        self.offset = timedelta(seconds=260)
        self.tago.world["positions"][ROUTE] = [tago_bus(4)]
        third = worker.run_pass()
        self.assertEqual((third.eta_errors["derived"]["events_created"],
                          third.eta_errors["derived"]["samples_created"]), (1, 1))
        self.assertEqual(self.daejeon.calls("getBusPosByRtid"), 0)
        positions = self.query(
            "SELECT o.stop_seq, o.run_key, o.location IS NOT NULL, o.details ->> 'run_group', "
            "o.details ->> 'run_direction', o.id, f.requested_at, f.received_at, o.collected_at "
            "FROM app.transit_observations o JOIN app.api_fetches f ON f.id = o.fetch_id "
            "WHERE o.kind = 'position' AND o.route_id = %s ORDER BY o.id", (self.route_id,))
        self.assertEqual([row[:3] for row in positions], [(2, None, True), (2, None, True), (4, None, True)])
        run_group = positions[0][3]
        self.assertTrue(run_group.startswith(f"tago:{ROUTE}:{PLATE}:"))
        self.assertEqual({row[3] for row in positions}, {run_group}, "같은 운행")
        self.assertEqual({row[4] for row in positions}, {"0"})
        lower, upper = positions[1][6], positions[2][7]
        self.assertEqual(positions[2][8], upper, "관측 행의 수집 시각은 응답을 받은 시각이다")
        (event,) = self.query(
            "SELECT id, quality, event_at, run_key, stop_seq, stop_id, vehicle_no, event_key, details, collected_at "
            "FROM app.transit_observations WHERE kind = 'event'")
        self.assertEqual(event[1:8], ("bounded", None, None, 3, self.stop_ids[3], PLATE,
                                      f"tago-position-arrival:{run_group}:3"))
        details = event[8]
        self.assertEqual((details["bounded_lower_at"], details["bounded_upper_at"], details["provider"],
                          details["rule"], details["basis"], details["source_observation_ids"]),
                         (lower.isoformat(), upper.isoformat(), "tago", "tago-position-arrival/1",
                          "snapshot_before_to_snapshot_at_or_after", [positions[1][5], positions[2][5]]))
        self.assertEqual(event[9], upper)
        (sample,) = self.query(
            "SELECT run_group, eta_source, horizon_bucket, quality, arrival_lower_at, arrival_upper_at, "
            "predicted_arrival_at, error_lower_seconds, error_upper_seconds, event_observation_id, "
            "details ->> 'position_source', details ->> 'event_rule' FROM app.eta_error_samples")
        self.assertEqual(sample[:6], (run_group, "provider", "120-300", "bounded", lower, upper))
        self.assertEqual(sample[6], now + timedelta(seconds=90))
        self.assertAlmostEqual(sample[7], (lower - sample[6]).total_seconds(), places=3)
        self.assertAlmostEqual(sample[8], (upper - sample[6]).total_seconds(), places=3)
        self.assertEqual(sample[9:], (event[0], "tago", "tago-position-arrival/1"))

        # ---- 다시 돌려도 늘지 않고, 현황에는 원천별로 나온다.
        self.offset = timedelta(seconds=390)
        fourth = worker.run_pass()
        self.assertEqual((fourth.eta_errors["derived"]["events_created"],
                          fourth.eta_errors["derived"]["samples_created"]), (0, 0))
        status = eta_error_status.report(self.settings, self.db, None)
        collection = status["collection"]
        self.assertEqual((collection["position_records"]["count"], collection["position_records"]["runs"],
                          collection["position_records"]["by_source"]["tago"]["count"],
                          collection["position_records"]["by_source"]["daejeon"]["count"]), (4, 1, 4, 0))
        self.assertEqual((collection["arrival_events"]["by_quality"],
                          collection["arrival_events"]["by_source"]["tago"]["by_quality"]),
                         ({"bounded": 1}, {"bounded": 1}))
        self.assertEqual(status["sampling"]["position_source"], "tago")
        described = describe_targets(self.settings, self.db)
        self.assertEqual({(t["kind"], t["interval_seconds"]) for t in described["targets"]},
                         {("positions", 60.0), ("arrivals", 60.0)})

    def test_no_calls_outside_hours_after_the_daily_limit_or_with_a_config_error(self):
        hour = datetime.now(KST).hour
        start = (hour + 2) % 24
        self.use_settings(eta_error_sampling_hours=f"{start}-{start + 1}")
        outside = self.sampling_worker().run_pass()
        self.assertEqual((outside.targets, outside.eta_errors["status"], self.daejeon.requests),
                         (0, "outside_hours", []))

        self.use_settings()
        self.daejeon.buses = [bus(NODES[1], datetime.now(UTC) - timedelta(seconds=60))]
        self.sampling_worker().run_pass()
        self.assertEqual((self.daejeon.calls("getBusPosByRtid"), self.daejeon.calls("getArrInfoByStopID")), (1, 1))
        self.use_settings(eta_error_sampling_daily_call_limit=1)
        self.offset = timedelta(seconds=130)
        limited = self.sampling_worker().run_pass()
        self.assertEqual((limited.targets, limited.eta_errors["status"], limited.eta_errors["daily_call_limit_reached"]),
                         (0, "daily_call_limit_reached", {"arrivals": 1, "positions": 1}))
        self.assertEqual(len(self.daejeon.requests), 2, "상한에 닿으면 표본 수집 호출을 멈춘다")

        self.use_settings(eta_error_run_gap_seconds=30)
        broken = self.sampling_worker().run_pass()
        self.assertEqual((broken.targets, broken.eta_errors["status"]), (0, "config_error"))
        self.assertIn("BACKEND_ETA_ERROR_RUN_GAP_SECONDS", broken.eta_errors["reason"])
        self.assertEqual(len(self.daejeon.requests), 2)


if __name__ == "__main__":
    unittest.main()
