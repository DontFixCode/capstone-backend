"""개발용 TAGO 확인 URL의 조건부 등록·입력 검증·응답 구성·오류 처리를 네트워크 없이 검증한다.

모든 응답과 ID는 테스트에서 만든 최소한의 합성 데이터이며 실제 TAGO 수집 결과가 아니다.
"""

import importlib
import json
import logging
import os
import unittest
from unittest import mock
from urllib.parse import quote, quote_plus

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.devtools.router import router
from app.integrations.bus_api import TagoClient

FAKE_KEY = "Synthetic+DevKey/For==Tests13579"
KEY_FORMS = (FAKE_KEY, quote(FAKE_KEY, safe=""), quote_plus(FAKE_KEY))
CONTROLLED = ("BACKEND_ENV", "ENABLE_DEVTOOLS", "TAGO_SERVICE_KEY")
DEV_PATHS = {
    "/dev/transit/stops",
    "/dev/transit/stops/nearby",
    "/dev/transit/routes",
    "/dev/transit/routes/{route_id}",
    "/dev/transit/routes/{route_id}/stops",
    "/dev/transit/routes/{route_id}/positions",
    "/dev/transit/stops/{stop_id}/arrivals",
}


def synthetic_body(items, *, total=None, container=None, code="00"):
    """합성 TAGO JSON 본문 (실제 응답 아님)."""
    if container is None:
        container = "" if not items else {"item": items[0] if len(items) == 1 else items}
    body = {"items": container, "numOfRows": 10, "pageNo": 1, "totalCount": len(items) if total is None else total}
    return {"response": {"header": {"resultCode": code, "resultMsg": "NORMAL SERVICE."}, "body": body}}


def gateway_xml(code, message):
    return (
        "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
        f"<returnAuthMsg>{message}</returnAuthMsg><returnReasonCode>{code}</returnReasonCode>"
        "</cmmMsgHeader></OpenAPI_ServiceResponse>"
    )


def load_main(**env):
    """환경변수를 바꿔 app.main을 다시 불러온다. 실제 .env.api보다 프로세스 환경변수가 우선한다."""
    environ = {name: value for name, value in os.environ.items() if name.upper() not in CONTROLLED}
    environ.update({"TAGO_SERVICE_KEY": "", **env})
    with mock.patch.dict(os.environ, environ, clear=True):
        import app.main as main

        return importlib.reload(main)


class Transport:
    """요청을 기록하고 준비한 응답을 순서대로 돌려준다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


def fake_client(transport):
    return TagoClient(FAKE_KEY, transport=httpx.MockTransport(transport), max_retries=0, request_interval=0)


class RegistrationTest(unittest.TestCase):
    def tearDown(self):
        load_main(BACKEND_ENV="production", ENABLE_DEVTOOLS="false")

    def assert_not_registered(self, main):
        client = TestClient(main.app)
        self.assertEqual(client.get("/dev/transit/stops").status_code, 404)
        self.assertEqual(client.get("/dev/transit/routes/SYN_R1/stops").status_code, 404)
        self.assertFalse(DEV_PATHS & set(client.get("/openapi.json").json()["paths"]))
        self.assertEqual(client.get("/health").json(), {"status": "ok"})

    def test_defaults_keep_devtools_off(self):
        environ = {name: value for name, value in os.environ.items() if name.upper() not in CONTROLLED}
        with mock.patch.dict(os.environ, environ, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual((settings.env, settings.enable_devtools, settings.devtools_enabled), ("production", False, False))

    def test_disabled_flag_in_development(self):
        self.assert_not_registered(load_main(BACKEND_ENV="development", ENABLE_DEVTOOLS="false"))

    def test_production_ignores_enabled_flag(self):
        self.assert_not_registered(load_main(BACKEND_ENV="production", ENABLE_DEVTOOLS="true"))

    def test_development_with_flag_registers_seven_get_urls(self):
        main = load_main(BACKEND_ENV="development", ENABLE_DEVTOOLS="true")
        client = TestClient(main.app)
        paths = client.get("/openapi.json").json()["paths"]
        self.assertEqual(DEV_PATHS & set(paths), DEV_PATHS)
        for path in DEV_PATHS:
            self.assertEqual(list(paths[path]), ["get"])
            self.assertEqual(paths[path]["get"]["tags"], ["devtools: TAGO 확인"])
        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        self.assertEqual(client.get("/docs").status_code, 200)
        # 인증키가 없으면 입력 오류는 422, 올바른 입력은 외부 호출 없이 503
        self.assertEqual(client.get("/dev/transit/stops?num_of_rows=101").status_code, 422)
        response = client.get("/dev/transit/stops")
        self.assertEqual(response.status_code, 503)
        self.assertIn("TAGO_SERVICE_KEY", response.json()["detail"])
        self.assertIsNone(main.app.state.tago_client)

    def test_shared_client_is_closed_on_shutdown(self):
        main = load_main(BACKEND_ENV="development", ENABLE_DEVTOOLS="true")
        tago = fake_client(Transport(httpx.Response(200, json=synthetic_body([]))))
        main.app.state.tago_client = tago
        with TestClient(main.app) as client:
            self.assertEqual(client.get("/dev/transit/routes").status_code, 200)
            self.assertFalse(tago._http.is_closed)
        self.assertTrue(tago._http.is_closed)


class EndpointTest(unittest.TestCase):
    def make(self, *responses):
        transport = Transport(*responses)
        app = FastAPI()
        app.include_router(router)
        app.state.tago_client = fake_client(transport)
        return TestClient(app), transport

    def test_urls_map_to_tago_operations_and_official_parameters(self):
        cases = [
            ("/dev/transit/stops?stop_name=합성&stop_no=0101&page_no=2&num_of_rows=5", "BusSttnInfoInqireService/getSttnNoList",
             {"cityCode": "25", "nodeNm": "합성", "nodeNo": "0101", "pageNo": "2", "numOfRows": "5"}),
            ("/dev/transit/stops/nearby?lat=36.3504&lon=127.3845", "BusSttnInfoInqireService/getCrdntPrxmtSttnList",
             {"gpsLati": "36.3504", "gpsLong": "127.3845", "pageNo": "1", "numOfRows": "10"}),
            ("/dev/transit/routes?route_no=61-1", "BusRouteInfoInqireService/getRouteNoList",
             {"cityCode": "25", "routeNo": "61-1", "pageNo": "1", "numOfRows": "10"}),
            ("/dev/transit/routes/SYN_R1", "BusRouteInfoInqireService/getRouteInfoIem",
             {"cityCode": "25", "routeId": "SYN_R1"}),
            ("/dev/transit/routes/SYN_R1/stops?page_no=3", "BusRouteInfoInqireService/getRouteAcctoThrghSttnList",
             {"cityCode": "25", "routeId": "SYN_R1", "pageNo": "3", "numOfRows": "200"}),
            ("/dev/transit/routes/SYN_R1/stops?num_of_rows=500", "BusRouteInfoInqireService/getRouteAcctoThrghSttnList",
             {"cityCode": "25", "routeId": "SYN_R1", "pageNo": "1", "numOfRows": "500"}),
            ("/dev/transit/routes/SYN_R1/positions", "BusLcInfoInqireService/getRouteAcctoBusLcList",
             {"cityCode": "25", "routeId": "SYN_R1", "pageNo": "1", "numOfRows": "10"}),
            ("/dev/transit/stops/SYN_S1/arrivals", "ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList",
             {"cityCode": "25", "nodeId": "SYN_S1", "pageNo": "1", "numOfRows": "10"}),
        ]
        for url, operation, expected in cases:
            with self.subTest(url=url):
                client, transport = self.make(httpx.Response(200, json=synthetic_body([{"id": "SYN"}])))
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                (request,) = transport.requests
                self.assertEqual(request.url.path, f"/1613000/{operation}")
                sent = dict(request.url.params)
                self.assertEqual(sent.pop("serviceKey"), FAKE_KEY)
                self.assertEqual(sent, {**expected, "_type": "json"})
                meta = response.json()["meta"]
                self.assertEqual(meta["tago_endpoint"], f"/1613000/{operation}")
                self.assertEqual(meta["params"], {**expected, "_type": "json"})
                self.assertEqual((meta["http_attempts"], meta["http_status"], meta["result_code"]), (1, 200, "00"))
                self.assertTrue(meta["requested_at"].endswith("Z") and meta["received_at"].endswith("Z"))
                self.assertIsNone(meta["source_observed_at"])

    def test_requests_only_one_page_even_when_more_exist(self):
        client, transport = self.make(httpx.Response(200, json=synthetic_body([{"n": 1}, {"n": 2}], total=300)))
        response = client.get("/dev/transit/stops?num_of_rows=2")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(response.json()["meta"]["paging"], {"numOfRows": 10, "pageNo": 1, "totalCount": 300})

    def test_raw_response_is_returned_unchanged(self):
        cases = [
            (synthetic_body([]), "empty", 0),
            (synthetic_body([{"nodeid": "SYN_S1", "nodeno": 101}]), "ok", 1),
            (synthetic_body([{"nodeid": "SYN_S1", "nodeno": "0101"}, {"nodeid": "SYN_S2", "nodeno": 102}]), "ok", 2),
        ]
        for body, status, count in cases:
            with self.subTest(status=status, count=count):
                client, _ = self.make(httpx.Response(200, json=body))
                response = client.get("/dev/transit/stops/SYN_S1/arrivals")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["raw"], body)
                self.assertEqual((response.json()["meta"]["status"], response.json()["meta"]["item_count"]), (status, count))
                self.assertNotIn("error", response.json())

    def test_invalid_input_is_rejected_before_any_external_call(self):
        urls = [
            "/dev/transit/stops?page_no=0",
            "/dev/transit/stops?num_of_rows=0",
            "/dev/transit/stops?num_of_rows=101",
            "/dev/transit/routes/SYN_R1/stops?num_of_rows=501",
            "/dev/transit/routes/SYN_R1/positions?num_of_rows=101",
            "/dev/transit/stops?stop_name=",
            "/dev/transit/stops?stop_name=%20",
            "/dev/transit/stops/nearby?lat=91&lon=127",
            "/dev/transit/stops/nearby?lat=36&lon=-181",
            "/dev/transit/stops/nearby?lat=nan&lon=127",
            "/dev/transit/stops/nearby?lon=127",
            "/dev/transit/routes/%20",
            "/dev/transit/routes/SYN%20R1/positions",
            "/dev/transit/stops/" + "S" * 31 + "/arrivals",
        ]
        client, transport = self.make()
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 422)
        self.assertEqual(transport.requests, [])

    def test_errors_map_to_status_codes_without_retry(self):
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        cases = [
            (httpx.Response(200, json=synthetic_body([], code="99")), 502, "provider", "99"),
            (httpx.Response(200, text=gateway_xml("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")), 502, "auth", "30"),
            (httpx.Response(200, text=gateway_xml("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")), 429, "rate_limit", "22"),
            (httpx.Response(200, json=synthetic_body([], container="unexpected")), 502, "unexpected_response", None),
            (httpx.Response(503, text="busy"), 502, "http", None),
            (timeout, 504, "timeout", None),
        ]
        for response, status_code, category, code in cases:
            with self.subTest(category=category):
                client, transport = self.make(response, httpx.Response(200, json=synthetic_body([])))
                result = client.get("/dev/transit/routes/SYN_R1/positions")
                self.assertEqual(result.status_code, status_code)
                self.assertEqual((result.json()["error"]["category"], result.json()["error"]["code"]), (category, code))
                self.assertEqual((len(transport.requests), result.json()["meta"]["http_attempts"]), (1, 1))
                self.assertEqual(result.json()["meta"]["status"], "error")

    def test_missing_key_returns_503_without_external_call(self):
        environ = {name: value for name, value in os.environ.items() if name.upper() not in CONTROLLED}
        with mock.patch.dict(os.environ, environ, clear=True):
            settings = Settings(_env_file=None)
        app = FastAPI()
        app.include_router(router)
        app.state.settings = settings
        app.state.tago_client = None
        response = TestClient(app).get("/dev/transit/routes")
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(app.state.tago_client)

    def test_key_is_not_exposed_in_responses_or_logs(self):
        echoed = f"rejected serviceKey={quote(FAKE_KEY, safe='')} key={FAKE_KEY}"
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            client, _ = self.make(
                httpx.Response(500, text=echoed),
                httpx.Response(200, json=synthetic_body([{"echo": FAKE_KEY}])),
                httpx.Response(200, text=gateway_xml("30", f"bad {FAKE_KEY}")),
            )
            texts = [client.get("/dev/transit/routes").text for _ in range(3)]
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        combined = "\n".join(texts + records)
        for form in KEY_FORMS:
            self.assertNotIn(form, combined)
        self.assertNotIn("serviceKey", json.dumps([json.loads(text)["meta"] for text in texts]))


if __name__ == "__main__":
    unittest.main()
