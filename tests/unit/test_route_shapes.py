"""버스 노선 형상(ODsay) 단위 시험: 클라이언트 응답 해석, 정류소 대응, 호출 예산 안의 수집 흐름. DB·실제 외부 호출 없음.

응답 형식(searchBusLane의 lane[].busID·localBusID, loadLane의 lane[].section[].graphPos[].x·y, 요청 순서대로 오는 lane)은
2026-11-12 실응답에서 확인한 구조이고, 좌표와 ID는 시험용 가상 값이다.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app.core.config import Settings
from app.integrations import route_shapes_api as shapes
from app.integrations.odsay_api import OdsayClient, OdsayConfigError
from app.workflows.route_shapes import PROVIDER, RouteShapeCollector, SHAPE_NOT_PROVIDED_REASON

FAKE_KEY = "unit-test-odsay-key+/=="
NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
LAT = 36.35
# 동쪽으로 갔다가 같은 도로로 돌아오는 선(꼭짓점 간격 약 180 m).
OUT = [[round(127.380 + 0.002 * i, 6), LAT] for i in range(6)]
LINE_A = OUT + OUT[-2::-1]
# 북쪽으로 가는 다른 노선의 선.
LINE_B = [[127.400, round(36.300 + 0.002 * i, 6)] for i in range(6)]


def stops_a() -> list[tuple[int, float, float]]:
    """LINE_A를 따라 갈 때 3곳, 올 때 3곳(도로에서 약 3 m 옆)."""
    return [(1, 127.381, LAT + 0.00003), (2, 127.385, LAT + 0.00003), (3, 127.389, LAT + 0.00003),
            (4, 127.3885, LAT - 0.00003), (5, 127.3845, LAT - 0.00003), (6, 127.3805, LAT - 0.00003)]


def stops_b() -> list[tuple[int, float, float]]:
    return [(1, 127.40003, 36.301), (2, 127.40003, 36.305), (3, 127.40003, 36.309)]


def points(rows) -> list[shapes.StopPoint]:
    return [shapes.StopPoint(seq=seq, lon=lon, lat=lat) for seq, lon, lat in rows]


def lane_body(lines) -> dict:
    return {"result": {"lane": [{"class": 1, "type": 11, "section": [
        {"graphPos": [{"x": x, "y": y} for x, y in line]}]} for line in lines], "boundary": {}}}


class OdsayClientTest(unittest.TestCase):
    def client(self, handler) -> OdsayClient:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)
        return OdsayClient(FAKE_KEY, transport=httpx.MockTransport(record))

    def test_search_reads_local_route_ids(self):
        body = {"result": {"totalCount": 2, "lane": [
            {"busNo": "60", "busID": 31278, "localBusID": "DJB30300026", "busCityCode": 3000},
            {"busNo": "608", "busID": 31014, "localBusID": "DJB30300130", "busCityCode": 3000}]}}
        client = self.client(lambda request: httpx.Response(200, json=body))
        search = client.search_bus_lanes("60", city_code="3000")
        self.assertEqual((search.call.status, search.total_count), ("success", 2))
        self.assertEqual([(lane.bus_id, lane.local_bus_id) for lane in search.lanes],
                         [("31278", "DJB30300026"), ("31014", "DJB30300130")])
        query = self.requests[0].url.params
        self.assertEqual((query["busNo"], query["CID"], query["apiKey"]), ("60", "3000", FAKE_KEY))
        self.assertNotIn("startNO", query, "startNO=1은 첫 결과를 건너뛴다")
        self.assertNotIn("apiKey", search.call.params)

    def test_empty_search_and_error_bodies(self):
        client = self.client(lambda request: httpx.Response(200, json={"result": {"totalCount": 0}}))
        self.assertEqual(client.search_bus_lanes("Z", city_code="3000").call.status, "empty")
        listed = {"error": [{"code": "500", "message": f"[ApiKeyAuthFailed] apiKey={FAKE_KEY}"}]}
        call = self.client(lambda request: httpx.Response(200, json=listed)).search_bus_lanes("1", city_code="3000").call
        self.assertEqual((call.status, call.provider_code), ("error", "500"))
        self.assertNotIn(FAKE_KEY, call.error + call.body)
        single = {"error": {"msg": "필수 입력값 형식 및 범위 오류", "code": "-8"}}
        call = self.client(lambda request: httpx.Response(200, json=single)).load_lanes(["1"]).call
        self.assertEqual((call.status, call.provider_code), ("error", "-8"))
        call = self.client(lambda request: httpx.Response(503, text="busy")).load_lanes(["1"]).call
        self.assertEqual((call.status, call.error), ("error", "http: HTTP 503"))

    def test_load_lanes_keeps_request_order(self):
        client = self.client(lambda request: httpx.Response(200, json=lane_body([LINE_A, LINE_B])))
        result = client.load_lanes(["31014", "31278"])
        self.assertEqual(self.requests[0].url.params["mapObject"], "0:0@31014:1:-1:-1@31278:1:-1:-1")
        self.assertEqual((result.call.status, result.lines), ("success", [LINE_A, LINE_B]))

    def test_lane_count_mismatch_is_partial(self):
        client = self.client(lambda request: httpx.Response(200, json=lane_body([LINE_A])))
        result = client.load_lanes(["31014", "31278"])
        self.assertEqual((result.call.status, result.lines), ("partial", [LINE_A]))
        empty = self.client(lambda request: httpx.Response(200, json=lane_body([]))).load_lanes(["31014"])
        self.assertEqual((empty.call.status, empty.lines), ("empty", []))

    def test_timeout_and_missing_key(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)
        self.assertEqual(self.client(timeout).load_lanes(["1"]).call.status, "timeout")
        with self.assertRaises(OdsayConfigError):
            OdsayClient.from_settings(Settings(_env_file=None))


class StopMatchTest(unittest.TestCase):
    def test_out_and_back_stops_follow_visit_order(self):
        matches = shapes.match_stops(LINE_A, points(stops_a()))
        segments = [m.segment for m in matches]
        self.assertEqual(segments, sorted(segments))
        self.assertTrue(all(s < 5 for s in segments[:3]), "갈 때 정류소는 가는 구간")
        self.assertTrue(all(s >= 5 for s in segments[3:]), "올 때 정류소는 돌아오는 구간")
        self.assertTrue(all(m.distance_m < 5 for m in matches))

    def test_entry_inserts_stop_vertices_and_slices(self):
        entry = shapes.build_pattern_shape(LINE_A, points(stops_a()), source="odsay", source_route_id="31014",
                                           fetched_at=NOW.isoformat())
        self.assertEqual((entry["status"], entry["reason"]), ("available", None))
        self.assertEqual(len(entry["geometry"]["coordinates"]), len(LINE_A) + 6)
        index = [entry["stop_vertex_index"][str(seq)] for seq in range(1, 7)]
        self.assertEqual(index, sorted(set(index)))
        self.assertEqual(entry["stop_match"]["matched"], 6)
        self.assertTrue(entry["version"].startswith("sha256:"))
        segment = shapes.segment_geometry(shapes.pattern_entry({"patterns": {"P": entry}}, "P"), 2, 5)
        coords = segment["geometry"]["coordinates"]
        self.assertEqual(segment["geometry_status"], "available")
        self.assertEqual((coords[0], coords[-1]), ([127.385, LAT], [127.3845, LAT]))
        self.assertIn([127.39, LAT], coords, "회차 지점을 지난다")

    def test_far_stop_is_left_unmapped(self):
        rows = stops_a()
        rows[1] = (2, 127.385, LAT + 0.003)     # 도로에서 약 330 m
        entry = shapes.build_pattern_shape(LINE_A, points(rows), source="odsay", source_route_id="1", fetched_at="t")
        self.assertEqual(entry["status"], "error")           # 6개 중 5개(83%)만 대응 → 패턴 불일치
        rows += [(7, 127.3803, LAT), (8, 127.3802, LAT), (9, 127.3801, LAT), (10, 127.38005, LAT)]
        entry = shapes.build_pattern_shape(LINE_A, points(rows), source="odsay", source_route_id="1", fetched_at="t")
        self.assertEqual((entry["status"], entry["stop_match"]["unmatched_seqs"]), ("available", [2]))
        self.assertNotIn("2", entry["stop_vertex_index"])
        missing = shapes.segment_geometry(shapes.pattern_entry({"patterns": {"P": entry}}, "P"), 2, 5)
        self.assertEqual((missing["geometry_status"], missing["geometry_reason"]),
                         ("unavailable", shapes.STOP_MAPPING_MISSING_REASON))

    def test_other_route_line_fails_alignment(self):
        entry = shapes.build_pattern_shape(LINE_B, points(stops_a()), source="odsay", source_route_id="1",
                                           fetched_at="t")
        self.assertEqual((entry["status"], entry["geometry"], entry["reason"]),
                         ("error", None, shapes.STOP_ALIGNMENT_FAILED_REASON))

    def test_entry_reasons_and_provider_setting(self):
        info = {"provider": {"name": "odsay", "status": "not_found", "route_id": None}}
        self.assertEqual(shapes.pattern_entry(info, "P")["reason"], shapes.ROUTE_NOT_IN_PROVIDER_REASON)
        info = {"provider": {"name": "odsay", "status": "matched", "route_id": "31014"}}
        entry = shapes.pattern_entry(info, "P")
        self.assertEqual((entry["reason"], entry["source"]), (shapes.PATTERN_MISSING_REASON, "odsay"))
        self.assertEqual(shapes.pattern_entry({}, "P")["reason"], shapes.NOT_CONFIGURED_REASON)
        self.assertEqual(shapes.configured_provider(" ODsay "), "odsay")
        self.assertIsNone(shapes.configured_provider("kakao"))
        self.assertIsNone(shapes.configured_provider(None))
        settings = Settings(_env_file=None, route_shape_provider=" ", ODSAY_API_KEY="", odsay_daily_call_limit="")
        self.assertEqual((settings.route_shape_provider, settings.odsay_api_key, settings.odsay_daily_call_limit),
                         (None, None, None))


class MemoryStore:
    def __init__(self, routes, stop_rows):
        self._routes = routes
        self._stops = stop_rows
        self.saved: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def routes(self):
        return [{**r, "provider": r["provider"] and dict(r["provider"]), "patterns": dict(r["patterns"])}
                for r in self._routes]

    def stop_points(self, route_ids):
        return {i: self._stops[i] for i in route_ids if i in self._stops}

    def _route(self, route_id):
        return next(r for r in self._routes if r["id"] == route_id)

    def save_provider(self, route_id, provider):
        self._route(route_id)["provider"] = dict(provider)

    def save_patterns(self, route_id, provider, patterns):
        route = self._route(route_id)
        route["provider"] = dict(provider)
        route["patterns"] = {k: {"source": e["source"], "status": e["status"], "fetched_at": e["fetched_at"]}
                             for k, e in patterns.items()}
        self.saved[route_id] = patterns

    def record_call(self, call, target_key, total_count):
        self.calls.append((target_key, call.status))


class CollectorTest(unittest.TestCase):
    """가상 ODsay: 노선 608(A)·60(B)·601(형상 없음)이 있고 '1'번(DJB1)은 없다.

    검색은 displayCnt개까지만 돌려준다. 시험에서 SEARCH_DISPLAY_COUNT를 줄이면 첫 글자 검색이 잘리는 경우가 된다.
    """

    def setUp(self):
        self.catalog = {"608": ("31014", "DJB608"), "60": ("31278", "DJB60"), "601": ("31198", "DJB601")}
        self.lines = {"31014": LINE_A, "31278": LINE_B}
        self.fail_calls = False
        self.too_many: int | None = None
        self.requests: list[tuple[str, str]] = []
        routes = [
            {"id": 1, "external_id": "DJB608", "route_no": "608", "provider": None, "patterns": {}},
            {"id": 2, "external_id": "DJB60", "route_no": "60", "provider": None, "patterns": {}},
            {"id": 3, "external_id": "DJB601", "route_no": "601", "provider": None, "patterns": {}},
            {"id": 4, "external_id": "DJB1", "route_no": "1", "provider": None, "patterns": {}},
        ]
        self.store = MemoryStore(routes, {1: {"pa": stops_a()}, 2: {"pb": stops_b()}, 3: {"pc": stops_b()},
                                          4: {"pd": stops_a()}})
        self.now = NOW

    def handler(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        params = request.url.params
        if self.fail_calls:
            return httpx.Response(200, json={"error": [{"code": "429", "message": "limit"}]})
        if operation == "searchBusLane":
            self.requests.append((operation, params["busNo"]))
            lanes = [{"busNo": no, "busID": int(bus_id), "localBusID": local, "busCityCode": 3000}
                     for no, (bus_id, local) in sorted(  # 번호가 정확히 같은 노선이 맨 앞, 다음은 긴 번호부터
                         self.catalog.items(), key=lambda item: (item[0] != params["busNo"], -len(item[0]), item[0]))
                     if no.startswith(params["busNo"])][:int(params["displayCnt"])]
            return httpx.Response(200, json={"result": {"totalCount": len(lanes), "lane": lanes}})
        ids = [part.split(":")[0] for part in params["mapObject"].split("@")[1:]]
        self.requests.append((operation, ",".join(ids)))
        if self.too_many is not None and len(ids) > self.too_many:
            return httpx.Response(200, json={"error": {"msg": "too many", "code": "-8"}})
        return httpx.Response(200, json=lane_body([self.lines[i] for i in ids if i in self.lines]))

    def run_collector(self, max_calls: int, batch_size: int = 200, refresh_all: bool = False):
        client = OdsayClient(FAKE_KEY, transport=httpx.MockTransport(self.handler))
        collector = RouteShapeCollector(self.store, client, city_code="3000", batch_size=batch_size,
                                        refresh_seconds=30 * 86400, refresh_all=refresh_all,
                                        clock=lambda: self.now)
        return collector.run(max_calls)

    def providers(self):
        return {r["external_id"]: (r["provider"] or {}).get("status") for r in self.store._routes}

    def test_full_run_maps_fetches_and_resolves_missing_lane(self):
        report = self.run_collector(max_calls=10)
        self.assertEqual(self.providers(), {"DJB608": "matched", "DJB60": "matched", "DJB601": "matched",
                                            "DJB1": "not_found"})
        # 첫 글자 검색 2회 → 형상 1회(선이 2개만 옴: 정류소 대응으로 60·608의 선임을 확인) → 선이 없던 601만 재조회.
        self.assertEqual(self.requests, [("searchBusLane", "6"), ("searchBusLane", "1"),
                                         ("loadLane", "31278,31198,31014"), ("loadLane", "31198")])
        self.assertEqual((report.stopped, report.matched, report.not_found, report.shapes_saved),
                         (None, 3, ["DJB1"], 2))
        self.assertEqual((report.pending_mapping, report.pending_shapes), (0, 0))
        self.assertEqual(self.store.saved[1]["pa"]["status"], "available")
        self.assertEqual(self.store.saved[1]["pa"]["source_route_id"], "31014")
        self.assertEqual(self.store.saved[2]["pb"]["status"], "available")
        self.assertEqual((self.store.saved[3]["pc"]["status"], self.store.saved[3]["pc"]["reason"]),
                         ("unavailable", SHAPE_NOT_PROVIDED_REASON))
        self.assertNotIn("lane_batch_failed_at", self.store._route(3)["provider"])
        self.assertEqual(len(self.store.calls), len(report.calls))
        # 갱신 간격 안에서는 다시 호출하지 않는다.
        self.requests.clear()
        again = self.run_collector(max_calls=10)
        self.assertEqual((self.requests, again.calls, again.stopped), ([], [], None))

    def test_truncated_search_falls_back_to_full_number(self):
        with mock.patch("app.workflows.route_shapes.SEARCH_DISPLAY_COUNT", 2):
            report = self.run_collector(max_calls=10)      # '6' 검색이 608·601 2개로 꽉 차서 60이 잘린다
        self.assertEqual(self.requests, [("searchBusLane", "6"), ("searchBusLane", "1"),
                                         ("loadLane", "31198,31014"), ("loadLane", "31198"),
                                         ("searchBusLane", "60"), ("loadLane", "31278")])
        self.assertEqual((report.stopped, report.matched, report.not_found, report.shapes_saved),
                         (None, 3, ["DJB1"], 2))

    def test_budget_is_respected_and_next_run_continues(self):
        first = self.run_collector(max_calls=1)
        self.assertEqual((first.stopped, len(first.calls)), ("budget_exhausted", 1))
        self.assertEqual(self.providers(), {"DJB608": "matched", "DJB60": "matched", "DJB601": "matched",
                                            "DJB1": None})
        self.assertEqual((first.pending_mapping, first.pending_shapes), (1, 3))
        self.assertEqual(self.store.saved, {})
        second = self.run_collector(max_calls=2)       # 검색 1 + 형상 1에서 예산 소진(601 재조회가 남음)
        self.assertEqual((second.stopped, second.not_found, second.pending_mapping, second.pending_shapes),
                         ("budget_exhausted", ["DJB1"], 0, 1))
        self.assertIn("lane_batch_failed_at", self.store._route(3)["provider"])
        third = self.run_collector(max_calls=5)
        self.assertEqual(self.requests[3:], [("loadLane", "31198")])
        self.assertEqual((third.stopped, third.pending_mapping, third.pending_shapes), (None, 0, 0))

    def test_swapped_lines_are_matched_by_stops(self):
        del self.catalog["601"]
        self.lines = {"31014": LINE_B, "31278": LINE_A}       # 제공처가 두 노선의 선을 바꿔 준 경우
        report = self.run_collector(max_calls=10)
        # 순서대로는 정류소가 맞지 않으므로 남은 선에서 맞는 선을 찾는다. 추가 호출은 없다.
        self.assertEqual(self.requests[2:], [("loadLane", "31278,31014")])
        self.assertEqual(self.store.saved[1]["pa"]["geometry"]["coordinates"][0], LINE_A[0])
        self.assertEqual(self.store.saved[2]["pb"]["geometry"]["coordinates"][0], LINE_B[0])
        self.assertEqual((report.shapes_saved, report.not_found, report.shape_problems), (2, ["DJB601", "DJB1"], []))

    def test_oversized_request_is_split(self):
        self.too_many = 2                                       # 가상 제공처: 한 요청에 노선 2개까지만 처리
        with mock.patch("app.workflows.route_shapes.MIN_SPLIT_BATCH", 1):
            report = self.run_collector(max_calls=10)
        self.assertEqual([r for r in self.requests if r[0] == "loadLane"],
                         [("loadLane", "31278,31198,31014"), ("loadLane", "31278"), ("loadLane", "31198,31014"),
                          ("loadLane", "31198")])
        self.assertEqual((report.stopped, report.shapes_saved, report.pending_shapes), (None, 2, 0))
        self.assertEqual([c["status"] for c in report.calls if c["operation"] == "loadLane"],
                         ["error", "success", "partial", "empty"])

    def test_refresh_all_refetches_everything_and_keeps_one_call_for_shapes(self):
        del self.catalog["60"]
        self.run_collector(max_calls=10)                    # 608·601 연결, 60·1은 없음
        self.catalog["60"] = ("31278", "DJB60")
        for route_id in (2, 4):                             # 잘못 남긴 '없음' 표시를 되돌린 상태
            self.store._route(route_id)["provider"] = None
        self.requests.clear()
        report = self.run_collector(max_calls=2, refresh_all=True)
        # 검색 1회 뒤에는 형상 요청 1회분을 남기려고 검색을 멈추고, 이미 저장된 노선도 한 요청에 넣어 다시 받는다.
        self.assertEqual(self.requests, [("searchBusLane", "1"), ("loadLane", "31198,31014")])
        self.assertEqual((report.stopped, report.pending_mapping, self.providers()["DJB60"]),
                         ("budget_exhausted", 1, None))
        self.requests.clear()
        report = self.run_collector(max_calls=10, refresh_all=True)
        self.assertEqual(self.requests, [("searchBusLane", "6"), ("loadLane", "31278,31014"), ("loadLane", "31198")])
        self.assertEqual((report.stopped, report.shapes_saved, report.pending_mapping, report.pending_shapes),
                         (None, 2, 0, 0))

    def test_failed_call_stops_the_run(self):
        self.fail_calls = True
        report = self.run_collector(max_calls=10)
        self.assertEqual((report.stopped, len(report.calls)), ("call_failed", 1))
        self.assertEqual(self.store.calls[0][1], "error")
        self.assertEqual(set(self.providers().values()), {None})

    def test_changed_pattern_and_expiry_trigger_refetch(self):
        self.run_collector(max_calls=10)
        self.requests.clear()
        self.store._stops[1] = {"pa2": stops_a()}             # 경유 목록이 바뀌어 패턴 키가 달라짐
        report = self.run_collector(max_calls=10)
        self.assertEqual(self.requests, [("loadLane", "31014")])
        self.assertEqual(list(self.store.saved[1]), ["pa2"])
        self.assertEqual(report.shapes_saved, 1)
        self.requests.clear()
        self.now = NOW + timedelta(days=31)                   # 갱신 간격 경과: not_found 재검색 + 형상 전부 다시
        self.run_collector(max_calls=10, batch_size=2)
        # 묶음 [60, 601]은 선이 1개만 온다: 60은 정류소 대응으로 확인하고 선이 없는 601만 다시 받는다.
        self.assertEqual(self.requests, [("searchBusLane", "1"), ("loadLane", "31278,31198"), ("loadLane", "31014"),
                                         ("loadLane", "31198")])


class WorkerShapeHookTest(unittest.TestCase):
    def test_worker_refreshes_all_shapes_after_static_refresh(self):
        from app.workers.main import Worker
        settings = Settings(_env_file=None, route_shape_provider="odsay", ODSAY_API_KEY=FAKE_KEY,
                            odsay_daily_call_limit="30")
        worker = Worker(settings_factory=lambda: settings,
                        shape_client_factory=lambda s: OdsayClient(FAKE_KEY, transport=httpx.MockTransport(
                            lambda request: httpx.Response(500))))
        report = mock.Mock(calls=[], shapes_saved=0, pending_shapes=0, pending_mapping=0, stopped=None)
        with mock.patch("app.workflows.route_shapes.collect", return_value=report) as collect, \
                mock.patch("app.workers.main.asdict", return_value={}):
            result = worker.collect_route_shapes(settings, db=mock.sentinel.db)
        self.assertEqual(result, {"status": "done"})
        self.assertTrue(collect.call_args.kwargs["refresh_all"])
        unset = Settings(_env_file=None, route_shape_provider="odsay", ODSAY_API_KEY=FAKE_KEY)
        self.assertEqual(worker.collect_route_shapes(unset, db=mock.sentinel.db),
                         {"status": "skipped", "reason": "daily_call_limit_unset"})
        self.assertIsNone(worker.collect_route_shapes(Settings(_env_file=None), db=mock.sentinel.db))


class StaticResponseShapeTest(unittest.TestCase):
    def test_lane_points_must_be_coordinates(self):
        body = {"result": {"lane": [{"class": 1, "type": 11, "section": [{"graphPos": [{"x": "a", "y": 1}]}]}]}}
        client = OdsayClient(FAKE_KEY, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(body))))
        self.assertEqual(client.load_lanes(["1"]).call.status, "error")


if __name__ == "__main__":
    unittest.main()
