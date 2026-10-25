"""inspect_transit CLI의 계획·실행·저장·분석을 네트워크 없이 검증한다.

모든 응답은 테스트에서 만든 최소한의 합성 데이터이며 실제 TAGO 수집 결과가 아니다.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote, quote_plus

import httpx

from app.core.config import Settings
from scripts import inspect_transit
from scripts.inspect_transit_report import find_runs

FAKE_KEY = "Synthetic+CliKey/For==Tests987654321"
KEY_FORMS = (FAKE_KEY, quote(FAKE_KEY, safe=""), quote_plus(FAKE_KEY))

# 합성 데이터: SYN_S9는 정류소 목록에 없고, SYN_S2는 노선 SYN_R1에서 두 번 경유한다.
SYNTHETIC = {
    "getSttnNoList": [
        {"nodeid": "SYN_S1", "nodenm": "합성1", "nodeno": 1001, "gpslati": 36.35, "gpslong": 127.38},
        {"nodeid": "SYN_S2", "nodenm": "합성2", "nodeno": 1002, "gpslati": 36.351, "gpslong": 127.381},
        {"nodeid": "SYN_S3", "nodenm": "합성3", "nodeno": "", "gpslati": 0, "gpslong": 127.382},
    ],
    "getRouteNoList": [
        {"routeid": "SYN_R1", "routeno": 5, "routetp": "합성버스", "startnodenm": "합성1", "endnodenm": "합성2"},
        {"routeid": "SYN_R2", "routeno": "6", "routetp": "합성버스", "startnodenm": "합성2", "endnodenm": "합성3"},
    ],
    "getRouteInfoIem": [{"routeid": "SYN_R1", "routeno": 5, "intervaltime": 0}],
    "getRouteAcctoThrghSttnList": [
        {"routeid": "SYN_R1", "nodeid": "SYN_S1", "nodenm": "합성1", "nodeord": 1, "updowncd": 0, "gpslati": 36.35, "gpslong": 127.38},
        {"routeid": "SYN_R1", "nodeid": "SYN_S2", "nodenm": "합성2", "nodeord": 2, "updowncd": 0, "gpslati": 36.351, "gpslong": 127.381},
        {"routeid": "SYN_R1", "nodeid": "SYN_S9", "nodenm": "합성9", "nodeord": 3, "updowncd": 1, "gpslati": 36.36, "gpslong": 127.39},
        {"routeid": "SYN_R1", "nodeid": "SYN_S2", "nodenm": "합성2", "nodeord": 4, "updowncd": 1, "gpslati": 36.351, "gpslong": 127.381},
    ],
    "getRouteAcctoBusLcList": [
        {"routenm": 5, "nodeid": "SYN_S2", "nodeord": 2, "vehicleno": "합성00가0001", "gpslati": 36.3512, "gpslong": 127.3811},
        {"routenm": 5, "nodeid": "SYN_S1", "nodeord": 1, "vehicleno": "합성00가0002"},
    ],
    "getSttnAcctoArvlPrearngeInfoList": [
        {"nodeid": "SYN_S1", "routeid": "SYN_R1", "routeno": 5, "arrtime": 120, "arrprevstationcnt": 3},
        {"nodeid": "SYN_S1", "routeid": "SYN_R2", "routeno": "6", "arrtime": "곧 도착", "arrprevstationcnt": 0},
    ],
}


def synthetic_handler(calls):
    """요청 경로의 상세기능 이름으로 합성 목록을 골라 pageNo·numOfRows대로 잘라 돌려준다."""

    def respond(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        calls.append(operation)
        items = SYNTHETIC[operation]
        if "pageNo" in request.url.params:
            page, rows = int(request.url.params["pageNo"]), int(request.url.params["numOfRows"])
            items = items[(page - 1) * rows: page * rows]
        container = {"item": items[0] if len(items) == 1 else items} if items else ""
        body = {"items": container, "totalCount": len(SYNTHETIC[operation])}
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}})

    return respond


def settings(key=FAKE_KEY):
    environ = {name: value for name, value in os.environ.items() if name.upper() != "TAGO_SERVICE_KEY"}
    if key:
        environ["TAGO_SERVICE_KEY"] = key
    with mock.patch.dict(os.environ, environ, clear=True):
        return Settings(_env_file=None, tago_request_interval_seconds=0)


def forbid_network(test):
    """실제 HTTP 전송이 시도되면 실패시킨다."""
    patcher = mock.patch.object(httpx.HTTPTransport, "handle_request", side_effect=AssertionError("network call"))
    patcher.start()
    test.addCleanup(patcher.stop)


class CliTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "out"

    def run_cli(self, *argv, key=FAKE_KEY, handler=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        transport = httpx.MockTransport(handler) if handler else None
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = inspect_transit.run_cli(
                [*argv, "--output-dir", str(self.output)] if argv else [],
                settings=settings(key), transport=transport, sleep=lambda seconds: None,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def saved_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.output.rglob("*.json"))


class PlanAndSafetyTest(CliTest):
    def setUp(self):
        super().setUp()
        forbid_network(self)

    def test_default_run_prints_help_without_calls(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_help_exits_without_calls(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            inspect_transit.run_cli(["route-detail", "--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_plan_without_key_shows_targets_and_writes_nothing(self):
        code, out, _ = self.run_cli("route-detail", "--route-id", "SYN_R1", "--route-id", "SYN_R2", key=None)
        self.assertEqual(code, 0)
        self.assertIn("getRouteInfoIem routeId=SYN_R1", out)
        self.assertIn("예상 요청 수: 최소 4회", out)
        self.assertIn("미설정", out)
        self.assertFalse(self.output.exists())

    def test_plan_for_fixed_pages_gives_exact_count(self):
        code, out, _ = self.run_cli("stations", "--max-pages", "3", key=None)
        self.assertEqual(code, 0)
        self.assertIn("예상 요청 수: 1~3회", out)

    def test_execute_without_key_fails_before_any_call(self):
        code, _, err = self.run_cli("arrivals", "--node-id", "SYN_S1", "--execute", key=None)
        self.assertEqual(code, 2)
        self.assertIn("TAGO_SERVICE_KEY", err)
        self.assertFalse(self.output.exists())

    def test_routes_from_requires_all_routes(self):
        code, _, err = self.run_cli("route-detail", "--route-id", "SYN_R1", "--routes-from", "x")
        self.assertEqual(code, 2)
        self.assertIn("--all-routes", err)


class ExecuteTest(CliTest):
    def test_route_detail_saves_responses_metadata_and_summary_without_key(self):
        calls = []
        code, out, _ = self.run_cli("route-detail", "--route-id", "SYN_R1", "--rows", "2", "--execute",
                                    handler=synthetic_handler(calls))
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, ["getRouteInfoIem", "getRouteAcctoThrghSttnList", "getRouteAcctoThrghSttnList"])
        (run_dir,) = find_runs(self.output)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["status"], run["budget"]["used"]), ("success", 3))
        stops = json.loads(next(run_dir.glob("responses/*ThrghSttnList*.json")).read_text(encoding="utf-8"))
        self.assertEqual((stops["status"], stops["item_count"], len(stops["pages"])), ("success", 4, 2))
        self.assertEqual(stops["request"]["params"], {"cityCode": "25", "routeId": "SYN_R1"})
        self.assertIsNone(stops["source_observed_at"])
        page = stops["pages"][0]
        self.assertTrue(page["requested_at"].endswith("Z") and page["received_at"].endswith("Z"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["route_stop_order"]["repeated_visits"]["nodes"], 1)
        saved = self.saved_text()
        for form in KEY_FORMS:
            self.assertNotIn(form, saved)
            self.assertNotIn(form, out)

    def test_budget_exhaustion_stops_and_records_incomplete_targets(self):
        calls = []
        code, out, _ = self.run_cli(
            "route-detail", "--route-id", "SYN_R1", "--route-id", "SYN_R2", "--max-requests", "2", "--execute",
            handler=synthetic_handler(calls),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        run = json.loads((find_runs(self.output)[0] / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["status"], "partial")
        self.assertEqual(
            [(entry["target"], entry["reason"]) for entry in run["incomplete"]],
            [("routeId=SYN_R2", "budget_exhausted")] * 2,
        )

    def test_auth_error_aborts_remaining_targets(self):
        calls = []

        def gateway_error(request):
            calls.append(request.url.params["nodeId"])
            return httpx.Response(200, text=(
                "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
                "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
                "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
            ))

        code, out, _ = self.run_cli("arrivals", "--node-id", "SYN_S1", "--node-id", "SYN_S2", "--execute",
                                    handler=gateway_error)
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["SYN_S1"])
        run = json.loads((find_runs(self.output)[0] / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["status"], run["abort_reason"]), ("aborted", "auth"))
        self.assertEqual(run["errors"][0]["code"], "30")
        self.assertEqual(
            [(entry["target"], entry["reason"]) for entry in run["incomplete"]],
            [("nodeId=SYN_S1", "error"), ("nodeId=SYN_S2", "aborted:auth")],
        )


class AnalyzeTest(CliTest):
    def test_analyze_links_saved_runs_and_estimates_volume(self):
        handler = synthetic_handler([])
        for argv in (
            ("stations", "--all-pages", "--rows", "2"),
            ("routes", "--all-pages", "--rows", "2"),
            ("route-detail", "--route-id", "SYN_R1", "--rows", "2"),
            ("locations", "--route-id", "SYN_R1", "--rows", "2"),
            ("arrivals", "--node-id", "SYN_S1", "--rows", "2"),
        ):
            code, out, _ = self.run_cli(*argv, "--execute", handler=handler)
            self.assertEqual(code, 0, out)
        before = {path: path.read_bytes() for path in self.output.rglob("*.json")}

        forbid_network(self)
        code, out, _ = self.run_cli("analyze")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(find_runs(self.output)), 5, "analyze 결과는 조회 실행으로 섞이지 않음")
        self.assertTrue(all(path.read_bytes() == content for path, content in before.items()), "입력 결과 보존")
        (summary_file,) = self.output.glob("*_analyze/summary.json")
        summary = json.loads(summary_file.read_text(encoding="utf-8"))

        linkage = summary["stop_station_linkage"]
        self.assertEqual((linkage["unique_stop_nodeids"], linkage["matched"], linkage["unmatched"]), (3, 2, 1))
        self.assertTrue(linkage["station_list_complete"])
        self.assertEqual(summary["counts"]["stations"]["unique_ids"], 3)
        self.assertEqual(summary["fields"]["getSttnNoList"]["fields"]["nodeno"]["empty"], 1)
        self.assertEqual(summary["fields"]["getRouteInfoIem"]["fields"]["intervaltime"]["zero"], 1)
        order = summary["route_stop_order"]
        self.assertEqual(order["repeated_visits"]["sample"][0]["nodeord"], [2, 4])
        self.assertEqual(order["updowncd_values"], {"0": 2, "1": 2})
        locations = summary["bus_locations"]
        self.assertFalse(locations["response_has_routeid"])
        self.assertEqual(locations["nodeid_on_route_stops"], "2/2")
        self.assertTrue(locations["per_route"][0]["routenm_matches_route_list"])
        self.assertTrue(locations["per_route"][0]["request_routeId_in_route_list"])
        self.assertTrue(summary["arrivals"]["per_station"][0]["request_nodeId_in_station_list"])
        arrivals = summary["arrivals"]
        self.assertEqual((arrivals["arrtime"]["integer_convertible"], arrivals["arrtime"]["not_convertible"]), (1, 1))
        self.assertEqual(arrivals["arrprevstationcnt"]["zero"], 1)
        self.assertFalse(arrivals["vehicle_number_field_present"])
        self.assertEqual(summary["coordinates"]["getSttnNoList"]["zero"], 1)
        self.assertEqual(summary["coordinates"]["getRouteAcctoBusLcList"]["missing_or_empty"], 1)

        volume = summary["call_volume"]
        self.assertEqual(volume["assumptions"]["calls_per_target"], 144)
        static = volume["static_daily_fixed_full"]["service_totals"]
        self.assertEqual(static["BusSttnInfoInqireService"]["requests"], 2)
        self.assertEqual(
            (static["BusRouteInfoInqireService"]["requests"], static["BusRouteInfoInqireService"]["lower_bound"]), (6, True)
        )
        full = volume["realtime_fixed_full"]["service_totals"]
        self.assertEqual((full["BusLcInfoInqireService"]["requests"], full["ArvlInfoInqireService"]["requests"]), (288, 432))
        self.assertIsNone(volume["realtime_user_needed"]["service_totals"]["BusLcInfoInqireService"]["requests"])
        self.assertEqual(volume["observed_by_service"]["BusSttnInfoInqireService"]["http_attempts"], 2)

    def test_complete_list_is_not_replaced_by_later_partial_run(self):
        handler = synthetic_handler([])
        self.run_cli("stations", "--all-pages", "--rows", "2", "--execute", handler=handler)
        self.run_cli("stations", "--rows", "2", "--execute", handler=handler)
        forbid_network(self)
        code, out, _ = self.run_cli("analyze")
        self.assertEqual(code, 0, out)
        (summary_file,) = self.output.glob("*_analyze/summary.json")
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        self.assertEqual(summary["collections_used"]["superseded"], 1)
        self.assertTrue(summary["counts"]["stations"]["complete_list"])
        self.assertEqual(summary["counts"]["stations"]["unique_ids"], 3)

    def test_empty_realtime_results_do_not_count_as_page_evidence(self):
        def empty(request):
            body = {"items": "", "numOfRows": 100, "pageNo": 1, "totalCount": 0}
            return httpx.Response(200, json={"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}})

        self.run_cli("locations", "--route-id", "SYN_R1", "--execute", handler=empty)
        forbid_network(self)
        code, out, _ = self.run_cli("analyze", "--location-targets", "3")
        self.assertEqual(code, 0, out)
        (summary_file,) = self.output.glob("*_analyze/summary.json")
        needed = json.loads(summary_file.read_text(encoding="utf-8"))["call_volume"]["realtime_user_needed"]
        locations = needed["service_totals"]["BusLcInfoInqireService"]
        self.assertEqual((locations["requests"], locations["lower_bound"]), (3 * 144, True))

    def test_invalid_estimate_inputs_are_rejected(self):
        for argv in (["analyze", "--interval-minutes", "0"], ["analyze", "--active-hours", "25"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                inspect_transit.run_cli(argv)

    def test_user_needed_estimate_uses_given_targets_and_hours(self):
        self.run_cli("arrivals", "--node-id", "SYN_S1", "--execute", handler=synthetic_handler([]))
        forbid_network(self)
        code, out, _ = self.run_cli("analyze", "--arrival-targets", "40", "--active-hours", "18", "--daily-limit", "10000")
        self.assertEqual(code, 0, out)
        (summary_file,) = self.output.glob("*_analyze/summary.json")
        needed = json.loads(summary_file.read_text(encoding="utf-8"))["call_volume"]["realtime_user_needed"]
        arrivals = needed["service_totals"]["ArvlInfoInqireService"]
        self.assertEqual((arrivals["requests"], arrivals["ratio_to_daily_limit"]), (40 * 108, 0.432))


if __name__ == "__main__":
    unittest.main()
